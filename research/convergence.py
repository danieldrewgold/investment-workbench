"""
Multi-Run Convergence

Loads prior research results for a ticker and computes stable
estimates from the distribution of past runs. This solves the
"each run produces different results" problem.

For each driver component:
  - Computes median value across prior runs (robust to outliers)
  - Computes spread (IQR) as a real confidence score
  - Tight spread (IQR < 1.0) = high confidence
  - Wide spread (IQR > 3.0) = low confidence

The converged estimates can be used to:
  1. Anchor the current run (blend new Claude output with prior median)
  2. Replace Claude's confidence scores with empirical ones
  3. Flag when a new run's estimate is an outlier vs history
"""

import json
import glob
from pathlib import Path
from dataclasses import dataclass, field
from statistics import median, stdev


RESULTS_DIR = Path("data/results")


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
    def median_value(self) -> float:
        return median(self.values) if self.values else 0

    @property
    def mean_value(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0

    @property
    def spread(self) -> float:
        """IQR-like spread. Lower = more stable."""
        if len(self.values) < 3:
            return 999
        sorted_v = sorted(self.values)
        q1 = sorted_v[len(sorted_v) // 4]
        q3 = sorted_v[3 * len(sorted_v) // 4]
        return q3 - q1

    @property
    def empirical_confidence(self) -> float:
        """
        Confidence based on how stable the estimate is across runs.
        Tight spread = high confidence.
        """
        if len(self.values) < 3:
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
    eps_values = []

    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)

            # Collect EPS
            post_eps = data.get("post_eps")
            if post_eps is not None:
                eps_values.append(post_eps)

            # Collect driver components
            drivers = data.get("drivers", {})
            for dname, dinfo in drivers.items():
                for cname, cdata in dinfo.get("components", {}).items():
                    key = f"{dname}.{cname}"
                    if key not in component_data:
                        component_data[key] = ComponentHistory(driver=dname, component=cname)
                    component_data[key].values.append(cdata["value"])
                    if "confidence" in cdata:
                        component_data[key].confidences.append(cdata["confidence"])

        except (json.JSONDecodeError, IOError, KeyError):
            continue

    report.num_runs = len(eps_values)
    report.components = component_data
    report.eps_values = eps_values
    if eps_values:
        report.converged_eps_median = round(median(eps_values), 2)
        if len(eps_values) >= 3:
            sorted_eps = sorted(eps_values)
            q1 = sorted_eps[len(sorted_eps) // 4]
            q3 = sorted_eps[3 * len(sorted_eps) // 4]
            report.converged_eps_spread = round(q3 - q1, 2)

    return report


def anchor_brief_with_priors(brief, convergence: ConvergenceReport,
                              blend_weight: float = 0.3, verbose: bool = False):
    """
    Blend the current Claude brief's driver values with prior run medians.

    blend_weight: how much to weight the prior median (0.3 = 30% prior, 70% new).
    Higher weight = more stable but slower to adapt to new information.

    Also replaces Claude's confidence scores with empirical ones when
    we have enough history (>= 5 runs).

    Modifies brief.drivers in place. Returns list of adjustments made.
    """
    if convergence.num_runs < 3:
        if verbose:
            print(f"  Convergence: only {convergence.num_runs} prior runs, skipping anchoring")
        return []

    adjustments = []

    for d in brief.drivers:
        dname = d["name"]
        for c in d.get("components", []):
            cname = c["name"]
            key = f"{dname}.{cname}"

            history = convergence.components.get(key)
            if not history or history.count < 3:
                continue

            old_val = c["value"]
            prior_median = history.median_value

            # Blend: new_value = (1-w) * claude_value + w * prior_median
            blended = round((1 - blend_weight) * old_val + blend_weight * prior_median, 2)
            c["value"] = blended

            # Replace confidence with empirical if we have enough history
            if history.count >= 5:
                old_conf = c.get("confidence", 0.5)
                empirical = history.empirical_confidence
                # Weight toward empirical but don't completely ignore Claude
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
                })

                if verbose:
                    print(f"  Anchored {key}: Claude {old_val:+.1f} -> blended {blended:+.1f} "
                          f"(prior median {prior_median:+.1f}, {history.count} runs, "
                          f"spread {history.spread:.1f})")

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
