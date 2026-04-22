"""
Multi-Run Convergence

Loads prior research results for a ticker and computes stable
estimates from the distribution of past runs. This solves the
"each run produces different results" problem.

For each driver component:
  - Computes median value across prior runs (robust to outliers)
  - Uses IQR-based outlier trimming when N >= 5 (drops max + min)
  - Computes spread (IQR) as a real confidence score
  - Tight spread (IQR < 1.0) = high confidence
  - Wide spread (IQR > 3.0) = low confidence

Engages at 2+ prior runs (was 3+). With 2 runs we can't detect outliers
but we can still anchor against the prior average. Higher blend weight
(0.40) when N=2, lower (0.25) when N >= 5 because the larger sample is
noisier to trust wholesale.

The converged estimates can be used to:
  1. Anchor the current run (blend new Claude output with prior median)
  2. Replace Claude's confidence scores with empirical ones
  3. Flag when a new run's estimate is an outlier vs history
"""

import json
import glob
import re
from pathlib import Path
from dataclasses import dataclass, field
from statistics import median, stdev


def _normalize_driver_name(name: str) -> str:
    """
    Normalize driver names for fuzzy matching across runs.

    Claude produces slightly different driver names each run — "Revenue
    Growth" / "revenue_growth" / "revenue_growth_rate" are all the same
    concept. Collapsing to a canonical form lets convergence anchor
    across these variants.

    Rule: lowercase, replace spaces/hyphens/underscores with a single
    underscore, strip common verbose suffixes (_rate, _pct, _growth).
    "Revenue Growth" → "revenue"
    "revenue_growth" → "revenue"
    "revenue_growth_rate" → "revenue"
    "Same-Store Sales Growth" → "same_store_sales"
    """
    n = re.sub(r"[\s\-]+", "_", name.strip().lower())
    n = re.sub(r"_+", "_", n).strip("_")
    # Strip common verbose suffixes — helps collapse variants
    for suffix in ("_growth_rate", "_growth_pct", "_rate_pct", "_growth",
                    "_change_pct", "_change", "_rate", "_pct"):
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)]
            break
    return n


RESULTS_DIR = Path("data/results")


def _trimmed_values(values: list[float]) -> list[float]:
    """
    Return the values with outliers removed. Two-tier strategy:
      - N < 5: no trimming, return as-is
      - N >= 5: drop the min and max (conservative; handles single outlier)
      - N >= 8: additionally drop values outside 2.5 * IQR from median

    This protects against single wild-run noise (our observed LYV pattern:
    6 runs produced one $-1.29 and one $10.21 outlier) without discarding
    legitimate variance when the run set is consistent.
    """
    if len(values) < 5:
        return list(values)
    sorted_v = sorted(values)
    # Always trim at least the extreme min and max when N >= 5
    trimmed = sorted_v[1:-1]
    if len(values) < 8:
        return trimmed
    # For larger N, additionally drop values far outside the middle
    med = median(trimmed)
    q1 = trimmed[len(trimmed) // 4]
    q3 = trimmed[3 * len(trimmed) // 4]
    iqr = max(q3 - q1, 0.0001)
    fence = 2.5 * iqr
    return [v for v in trimmed if abs(v - med) <= fence]


@dataclass
class ComponentHistory:
    """History of one driver component across runs."""
    driver: str
    component: str
    values: list = field(default_factory=list)
    confidences: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def trimmed_values(self) -> list[float]:
        """Values with outliers removed via _trimmed_values heuristic."""
        return _trimmed_values(self.values)

    @property
    def median_value(self) -> float:
        """Median of trimmed values — robust to outlier runs."""
        trimmed = self.trimmed_values
        return median(trimmed) if trimmed else 0

    @property
    def raw_median_value(self) -> float:
        """Median of ALL values (no trimming) — for comparison / debugging."""
        return median(self.values) if self.values else 0

    @property
    def mean_value(self) -> float:
        """Mean of trimmed values."""
        trimmed = self.trimmed_values
        return sum(trimmed) / len(trimmed) if trimmed else 0

    @property
    def spread(self) -> float:
        """IQR-like spread of trimmed values. Lower = more stable.
        Computed on TRIMMED values so a single wild run doesn't inflate spread."""
        trimmed = self.trimmed_values
        if len(trimmed) < 2:
            return 999
        if len(trimmed) == 2:
            return abs(trimmed[0] - trimmed[1])
        sorted_v = sorted(trimmed)
        q1 = sorted_v[len(sorted_v) // 4]
        q3 = sorted_v[3 * len(sorted_v) // 4]
        return q3 - q1

    @property
    def empirical_confidence(self) -> float:
        """
        Confidence based on how stable the estimate is across runs.
        Tight spread = high confidence.
        """
        if len(self.values) < 2:
            return 0.5  # not enough data
        s = self.spread
        if s < 0.5:
            return 0.85
        elif s < 1.0:
            return 0.75
        elif s < 2.0:
            return 0.60
        elif s < 5.0:
            return 0.45
        else:
            return 0.30


@dataclass
class ConvergenceReport:
    """Summary of multi-run convergence for a ticker."""
    ticker: str
    num_runs: int
    components: dict = field(default_factory=dict)
    # {driver.component: ComponentHistory}
    driver_totals: dict = field(default_factory=dict)
    # {driver_name: ComponentHistory} — aggregated driver-level history
    # (sum of component values across each run). Used as fallback when
    # component names don't match exactly between current run and priors.
    converged_eps_median: float = 0
    converged_eps_spread: float = 0
    eps_values: list = field(default_factory=list)


def load_convergence(ticker: str, max_runs: int = 20) -> ConvergenceReport:
    """
    Load prior results and compute convergence statistics.

    Returns ConvergenceReport with per-component medians, spreads,
    and empirical confidence scores.
    """
    ticker = ticker.upper()
    report = ConvergenceReport(ticker=ticker, num_runs=0)

    if not RESULTS_DIR.exists():
        return report

    files = sorted(RESULTS_DIR.glob(f"{ticker}_*.json"), reverse=True)[:max_runs]
    if not files:
        return report

    component_data = {}  # {driver.component: ComponentHistory}
    driver_totals = {}   # {driver_name: ComponentHistory} — aggregate per run
    eps_values = []

    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)

            # Collect EPS
            post_eps = data.get("post_eps")
            if post_eps is not None:
                eps_values.append(post_eps)

            # Collect driver components AND aggregate per driver
            drivers = data.get("drivers", {})
            for dname, dinfo in drivers.items():
                # Component-level history (exact match)
                for cname, cdata in dinfo.get("components", {}).items():
                    key = f"{dname}.{cname}"
                    if key not in component_data:
                        component_data[key] = ComponentHistory(driver=dname, component=cname)
                    component_data[key].values.append(cdata["value"])
                    if "confidence" in cdata:
                        component_data[key].confidences.append(cdata["confidence"])

                # Driver-level aggregate (sum of component values),
                # indexed by NORMALIZED driver name so "Revenue Growth"
                # and "revenue_growth_rate" match.
                driver_total = dinfo.get("value")
                if driver_total is None:
                    driver_total = sum(
                        c.get("value", 0)
                        for c in dinfo.get("components", {}).values()
                    )
                norm_key = _normalize_driver_name(dname)
                if norm_key not in driver_totals:
                    driver_totals[norm_key] = ComponentHistory(
                        driver=norm_key, component="__total__"
                    )
                driver_totals[norm_key].values.append(driver_total)

        except (json.JSONDecodeError, IOError, KeyError):
            continue

    report.num_runs = len(eps_values)
    report.components = component_data
    report.driver_totals = driver_totals
    report.eps_values = eps_values
    if eps_values:
        # Trimmed median: protects against single-run noise (one $10.21 or
        # one $-1.29 doesn't drag the anchor).
        trimmed_eps = _trimmed_values(eps_values)
        report.converged_eps_median = round(median(trimmed_eps), 2) if trimmed_eps else 0
        if len(trimmed_eps) >= 3:
            sorted_eps = sorted(trimmed_eps)
            q1 = sorted_eps[len(sorted_eps) // 4]
            q3 = sorted_eps[3 * len(sorted_eps) // 4]
            report.converged_eps_spread = round(q3 - q1, 2)
        elif len(trimmed_eps) == 2:
            report.converged_eps_spread = round(abs(trimmed_eps[0] - trimmed_eps[1]), 2)

    return report


def _adaptive_blend_weight(num_runs: int, explicit: float | None = None) -> float:
    """
    Pick blend weight (how much to trust priors) based on how much history
    we have. Rationale:
      - N=2: priors are thin; trust them moderately (0.40) because new run
        is probably also noise
      - N=3-4: still small sample but starting to show stable range (0.35)
      - N=5+: enough data to trim outliers; don't let priors dominate over
        new information (0.25)
    """
    if explicit is not None:
        return explicit
    if num_runs <= 2:
        return 0.40
    if num_runs <= 4:
        return 0.35
    return 0.25


def anchor_brief_with_priors(brief, convergence: ConvergenceReport,
                              blend_weight: float | None = None,
                              verbose: bool = False):
    """
    Blend the current Claude brief's driver values with prior run medians.

    blend_weight: fraction weight on the prior median. If None (default),
    adaptive — heavier on priors when we have less data, lighter when we
    have enough runs for outlier trimming to be meaningful.

    Also replaces Claude's confidence scores with empirical ones when
    we have enough history (>= 5 runs).

    Engages at 2+ prior runs. With 2 runs the "median" is an average, but
    even that's better than treating each run independently.

    Modifies brief.drivers in place. Returns list of adjustments made.
    """
    if convergence.num_runs < 2:
        if verbose:
            print(f"  Convergence: only {convergence.num_runs} prior runs (need 2+), skipping")
        return []

    bw = _adaptive_blend_weight(convergence.num_runs, blend_weight)
    if verbose:
        print(f"  Convergence: {convergence.num_runs} prior runs, blend_weight={bw:.2f}")

    adjustments = []
    driver_level_anchored = set()

    for d in brief.drivers:
        dname = d["name"]
        components = d.get("components", [])
        anchored_any_component = False

        # First pass: try exact-match component anchoring
        for c in components:
            cname = c["name"]
            key = f"{dname}.{cname}"

            history = convergence.components.get(key)
            if not history or history.count < 2:
                continue

            old_val = c["value"]
            prior_median = history.median_value

            # Blend: new_value = (1-w) * claude_value + w * prior_median
            blended = round((1 - bw) * old_val + bw * prior_median, 2)
            c["value"] = blended
            anchored_any_component = True

            # Replace confidence with empirical if we have enough history
            if history.count >= 5:
                old_conf = c.get("confidence", 0.5)
                empirical = history.empirical_confidence
                c["confidence"] = round(0.6 * empirical + 0.4 * old_conf, 2)

            if abs(old_val - blended) > 0.05:
                adjustments.append({
                    "driver": dname,
                    "component": cname,
                    "claude_value": old_val,
                    "prior_median": round(prior_median, 2),
                    "blended_value": blended,
                    "spread": round(history.spread, 2),
                    "empirical_confidence": history.empirical_confidence,
                    "num_priors": history.count,
                    "trimmed_count": len(history.trimmed_values),
                    "match_level": "component",
                })

                if verbose:
                    n_trimmed = history.count - len(history.trimmed_values)
                    trim_note = f", {n_trimmed} outlier(s) trimmed" if n_trimmed else ""
                    print(f"  Anchored {key}: Claude {old_val:+.1f} -> blended {blended:+.1f} "
                          f"(prior median {prior_median:+.1f}, {history.count} runs"
                          f"{trim_note}, spread {history.spread:.1f})")

        # Second pass (fallback): if NO components anchored via exact match,
        # but we have driver-level history for this driver, scale the whole
        # driver's components proportionally toward the prior driver total.
        # This handles the case where Claude produces different component
        # names between runs (the LYV pattern — ticketing_segment_growth
        # one run, stadium_volume_growth the next, same parent "Revenue
        # Growth" driver).
        if anchored_any_component or not components:
            continue
        # Look up prior driver history by NORMALIZED name so "Revenue
        # Growth" matches a prior run's "revenue_growth" or "revenue_growth_rate"
        norm_dname = _normalize_driver_name(dname)
        driver_hist = convergence.driver_totals.get(norm_dname)
        if not driver_hist or driver_hist.count < 2:
            continue
        # Sum current run's component values
        current_total = sum(c.get("value", 0) for c in components)
        prior_total_median = driver_hist.median_value
        if current_total == 0:
            continue
        # Blend the DRIVER TOTAL, then scale each component proportionally
        blended_total = (1 - bw) * current_total + bw * prior_total_median
        scale = blended_total / current_total if current_total != 0 else 1.0
        if abs(scale - 1.0) < 0.02:
            continue  # sub-2% change — not worth logging
        for c in components:
            c["value"] = round(c["value"] * scale, 2)
        driver_level_anchored.add(dname)
        adjustments.append({
            "driver": dname,
            "component": "(driver-level aggregate)",
            "claude_value": round(current_total, 2),
            "prior_median": round(prior_total_median, 2),
            "blended_value": round(blended_total, 2),
            "spread": round(driver_hist.spread, 2),
            "num_priors": driver_hist.count,
            "match_level": "driver_aggregate",
            "scale_factor": round(scale, 3),
        })
        if verbose:
            print(f"  Anchored {dname} (driver aggregate fallback — component "
                  f"names didn't match priors): "
                  f"total Claude {current_total:+.1f} -> blended {blended_total:+.1f} "
                  f"(prior median {prior_total_median:+.1f}, "
                  f"{driver_hist.count} runs, scale {scale:.3f})")

    return adjustments


def flag_outliers(brief, convergence: ConvergenceReport) -> list[str]:
    """
    Flag components where the current run's value is an outlier
    relative to prior runs (>2 standard deviations from mean).
    """
    if convergence.num_runs < 5:
        return []

    flags = []
    for d in brief.drivers:
        for c in d.get("components", []):
            key = f"{d['name']}.{c['name']}"
            history = convergence.components.get(key)
            if not history or history.count < 5:
                continue

            try:
                sd = stdev(history.values)
                if sd > 0:
                    z_score = abs(c["value"] - history.mean_value) / sd
                    if z_score > 2.0:
                        flags.append(
                            f"{key}: {c['value']:+.1f} is {z_score:.1f} sigma from "
                            f"mean {history.mean_value:+.1f} (sd={sd:.1f})")
            except Exception:
                continue

    return flags
