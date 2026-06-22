"""Refresh the WARN-notice dataset cache (state mass-layoff filings).

Run weekly (or before a fresh research push). The heavy state-feed fetch lives
HERE, not in the per-run pipeline — the `workforce_signal` DAG step reads the
cached dataset and name-matches at runtime (fast). Extend coverage by adding
states to `warn_loader._SOURCES`.

    python refresh_warn.py
"""

from ingestion.loaders.warn_loader import fetch_warn_dataset

if __name__ == "__main__":
    recs = fetch_warn_dataset(force=True, verbose=True)
    by_state: dict[str, int] = {}
    for r in recs:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    print(f"\nWARN cache refreshed: {len(recs)} notices  {by_state}")
