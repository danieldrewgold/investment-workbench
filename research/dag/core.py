"""
DAG core — Step declarations, runner with parallel execution, cache.

A Step wraps an existing function and declares:
  - name: identifier
  - inputs: list of other Step names it depends on
  - run: callable(ctx: dict) -> Any, where ctx has {input_name: result}
  - cache_key: optional callable(ctx) -> str that returns a stable hash
    string for this step's inputs; if None, the step is never cached
  - is_serializable: whether the output can be JSON-round-tripped for cache
    (some steps return dataclasses that need custom handling; mark False
    to keep cache in-memory only)

The runner uses a thread pool. On each tick it finds steps whose deps are
all satisfied and dispatches them. When a step's output is ready its name
appears in the shared results dict so downstream steps can use it.

Failure policy: when a step raises or returns None-with-error, downstream
steps in that branch are marked skipped but unrelated branches keep going.
Final return includes a trace showing what ran, what cached, and what failed.
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


# --------------------------------------------------------------------------
# Core types
# --------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    inputs: list[str] = field(default_factory=list)
    run: Callable[[dict], Any] = None
    # If provided, returns a stable hash string used as the cache key.
    # The step's output will be cached to disk keyed on this hash.
    cache_key: Callable[[dict], str] | None = None
    # If False, cache is in-memory only for this run (skip disk serialization).
    # Use for steps that return non-JSON-serializable objects (dataclasses
    # with nested objects, etc.).
    is_serializable: bool = True


@dataclass
class StepResult:
    name: str
    status: str            # "ok" | "cached" | "skipped" | "failed"
    duration_seconds: float = 0.0
    cache_key_hash: str = ""
    error: str = ""
    output_preview: str = ""   # first 200 chars of repr(output)


@dataclass
class DagTrace:
    ticker: str
    started_at: str
    finished_at: str = ""
    total_duration_seconds: float = 0.0
    steps: list[StepResult] = field(default_factory=list)
    final_results_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _cache_dir_for(ticker: str) -> Path:
    return Path("data/dag_cache") / ticker.upper()


def _cache_path(ticker: str, step_name: str, cache_key: str) -> Path:
    return _cache_dir_for(ticker) / f"{step_name}_{cache_key}.json"


def _load_from_cache(ticker: str, step_name: str, cache_key: str) -> Any | None:
    """Returns the cached output, or None if not present / unreadable."""
    path = _cache_path(ticker, step_name, cache_key)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("output")
    except Exception:
        return None


def _write_to_cache(ticker: str, step_name: str, cache_key: str, output: Any) -> None:
    path = _cache_path(ticker, step_name, cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = {
            "step": step_name,
            "cache_key": cache_key,
            "written_at": datetime.utcnow().isoformat() + "Z",
            "output": output,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception:
        # Cache write failures are non-fatal — next run just recomputes
        pass


# --------------------------------------------------------------------------
# Hash helpers (for building cache keys inside step definitions)
# --------------------------------------------------------------------------

def stable_hash(*parts: Any) -> str:
    """Hash a sequence of parts into a short stable string."""
    h = hashlib.sha256()
    for p in parts:
        if p is None:
            h.update(b"<none>")
        elif isinstance(p, (str, int, float, bool)):
            h.update(repr(p).encode("utf-8", errors="replace"))
        elif isinstance(p, bytes):
            h.update(p)
        else:
            h.update(json.dumps(p, sort_keys=True, default=str).encode("utf-8", errors="replace"))
        h.update(b"|")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def _topo_order(steps: list[Step]) -> list[str]:
    """Return step names in a valid topological order. Raises on cycles."""
    by_name = {s.name: s for s in steps}
    in_degree = {s.name: 0 for s in steps}
    deps: dict[str, list[str]] = {s.name: [] for s in steps}
    for s in steps:
        for inp in s.inputs:
            if inp not in by_name:
                raise ValueError(f"Step {s.name!r} depends on unknown step {inp!r}")
            deps[inp].append(s.name)
            in_degree[s.name] += 1

    ready = [name for name, deg in in_degree.items() if deg == 0]
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for child in deps[n]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    if len(order) != len(steps):
        raise ValueError("cycle detected in step graph")
    return order


def run_dag(
    steps: list[Step],
    *,
    ticker: str,
    max_parallel: int = 4,
    context: dict | None = None,
    read_cache: bool = True,
    write_cache: bool = True,
    use_cache: bool | None = None,
    verbose: bool = False,
    write_trace: bool = True,
) -> tuple[dict, DagTrace]:
    """
    Execute a list of Step declarations. Returns (results, trace).

    Args:
        steps: the step declarations
        ticker: used for cache dir + trace filename
        max_parallel: thread pool size for parallel step execution
        context: initial context values (e.g., {"ticker": "CMG"})
            that seeded steps can reference via their run() callable
        read_cache: if False, bypass cache reads and recompute everything
            (but still writes fresh results to cache for next time)
        write_cache: if False, don't persist step outputs to cache
        use_cache: deprecated — if provided, sets both read_cache and
            write_cache. Kept for backward compat.
        verbose: print per-step status lines
        write_trace: write the final trace JSON to data/dag_traces/

    Each step.run receives a dict of {input_step_name: output} for its
    declared inputs, plus the shared `context` entries. Steps without
    a cache_key are always run fresh.
    """
    # Back-compat: use_cache=False turns OFF both read and write
    if use_cache is not None:
        read_cache = use_cache
        write_cache = use_cache
    by_name = {s.name: s for s in steps}
    topo = _topo_order(steps)
    results: dict[str, Any] = dict(context or {})
    step_statuses: dict[str, str] = {}   # name -> "ok" | "failed" | "skipped"
    trace = DagTrace(
        ticker=ticker.upper(),
        started_at=datetime.utcnow().isoformat() + "Z",
    )
    started_clock = time.time()

    def _step_failed_upstream(name: str) -> bool:
        """True if any ancestor has failed or been skipped."""
        for inp in by_name[name].inputs:
            if step_statuses.get(inp) in ("failed", "skipped"):
                return True
        return False

    def _try_cached(step: Step, ctx_for_step: dict) -> tuple[bool, Any, str]:
        """Return (cache_hit, output, cache_key_hash) or (False, None, key)."""
        if not use_cache or step.cache_key is None or not step.is_serializable:
            return False, None, ""
        try:
            key = step.cache_key(ctx_for_step)
        except Exception:
            return False, None, ""
        cached = _load_from_cache(ticker, step.name, key)
        if cached is None:
            return False, None, key
        return True, cached, key

    # Execute in waves. Within each wave, run all ready steps in parallel.
    remaining = list(topo)
    executor = ThreadPoolExecutor(max_workers=max_parallel)

    try:
        while remaining:
            # Identify steps whose deps are satisfied
            wave: list[Step] = []
            still_pending: list[str] = []
            for name in remaining:
                step = by_name[name]
                if all(inp in results or step_statuses.get(inp) in ("failed", "skipped")
                       for inp in step.inputs):
                    wave.append(step)
                else:
                    still_pending.append(name)
            if not wave:
                # Shouldn't happen if topo was valid, but guard against deadlock
                if verbose:
                    print(f"  [DAG] deadlock — pending: {still_pending}")
                break
            remaining = still_pending

            # Dispatch wave — collect (future, step, cache_key) so write
            # uses the SAME key that read computed. Re-computing the key
            # post-run leaks the step's own output into its cache key and
            # breaks the cache.
            futures: dict[Future, tuple[Step, str]] = {}
            for step in wave:
                # Skip if upstream failed
                if _step_failed_upstream(step.name):
                    step_statuses[step.name] = "skipped"
                    trace.steps.append(StepResult(
                        name=step.name, status="skipped",
                        error="upstream dependency failed or was skipped",
                    ))
                    if verbose:
                        print(f"  [DAG] {step.name}: SKIPPED (upstream failed)")
                    continue

                # Build the ctx this step sees: only its declared inputs
                # + any initial context values. Don't include outputs from
                # sibling steps — those aren't this step's inputs.
                ctx_for_step = {inp: results.get(inp) for inp in step.inputs}
                # Also include seeded context entries (e.g., "ticker")
                if context:
                    for k, v in context.items():
                        ctx_for_step.setdefault(k, v)

                # Compute the cache key ONCE from inputs + initial context
                planned_cache_key = ""
                if step.cache_key is not None:
                    try:
                        planned_cache_key = step.cache_key(ctx_for_step)
                    except Exception:
                        planned_cache_key = ""

                # Try cache
                if read_cache and step.is_serializable and planned_cache_key:
                    cached = _load_from_cache(ticker, step.name, planned_cache_key)
                    if cached is not None:
                        results[step.name] = cached
                        step_statuses[step.name] = "ok"
                        trace.steps.append(StepResult(
                            name=step.name, status="cached",
                            cache_key_hash=planned_cache_key,
                            output_preview=repr(cached)[:200],
                        ))
                        if verbose:
                            print(f"  [DAG] {step.name}: CACHED ({planned_cache_key})")
                        continue

                # Submit for execution
                def _invoke(s=step, ctx=ctx_for_step):
                    start = time.time()
                    try:
                        out = s.run(ctx)
                        return "ok", out, time.time() - start, ""
                    except Exception as e:
                        tb = traceback.format_exc()[-400:]
                        return "failed", None, time.time() - start, f"{type(e).__name__}: {e}\n{tb}"

                fut = executor.submit(_invoke)
                futures[fut] = (step, planned_cache_key)

            # Wait for this wave's futures
            for fut, (step, planned_cache_key) in futures.items():
                status, output, dur, err = fut.result()
                step_statuses[step.name] = status
                if status == "ok":
                    results[step.name] = output
                    # Write to cache using the SAME key used on read
                    if write_cache and step.is_serializable and planned_cache_key:
                        _write_to_cache(ticker, step.name, planned_cache_key, output)
                    if verbose:
                        print(f"  [DAG] {step.name}: OK ({dur:.1f}s)")
                else:
                    if verbose:
                        print(f"  [DAG] {step.name}: FAILED ({dur:.1f}s) — "
                              f"{err.splitlines()[0] if err else 'unknown'}")

                trace.steps.append(StepResult(
                    name=step.name,
                    status=status,
                    duration_seconds=round(dur, 3),
                    cache_key_hash=planned_cache_key,
                    error=err if status == "failed" else "",
                    output_preview=repr(output)[:200] if output is not None else "",
                ))
    finally:
        executor.shutdown(wait=True)

    trace.finished_at = datetime.utcnow().isoformat() + "Z"
    trace.total_duration_seconds = round(time.time() - started_clock, 2)
    trace.final_results_keys = sorted([k for k in results.keys() if k not in (context or {})])

    if write_trace:
        try:
            Path("data/dag_traces").mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            trace_path = Path("data/dag_traces") / f"{ticker.upper()}_{ts}.json"
            with open(trace_path, "w", encoding="utf-8") as f:
                json.dump(trace.to_dict(), f, indent=2, default=str)
            if verbose:
                print(f"  [DAG] trace: {trace_path}")
        except Exception:
            pass

    return results, trace
