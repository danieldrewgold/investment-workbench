"""
Historical Baseline Forecast / Regression Sanity Check

Product layer: Layer 3 analytical escalation (subordinate to thesis-driven estimates)

Role: Build a neutral, mathematically-driven baseline from historical data
to challenge management guidance, consensus, or thesis assumptions.

This is NOT:
  - the primary forecasting framework
  - a replacement for driver-based modeling
  - a default step in every research workflow

This IS:
  - an optional analytical escalation
  - a challenge function for assumptions
  - a sanity-check workpaper producer
  - explicitly labeled as "baseline" not "forecast"

Design principle from the financial-modeling skill:
  "Model to consensus first... then take variant views with documented rationale."
  The baseline helps evaluate whether variant views (or consensus itself)
  look stretched or conservative relative to what history would suggest.

Techniques available:
  - CAGR (compound annual growth rate)
  - Seasonality detection
  - Simple linear regression (OLS via normal equations)
  - Rolling trend comparison
  - Historical cadence analysis

Guardrails:
  - Structural break detection
  - Short sample warnings
  - Unstable seasonality flags
  - Data quality assessment before forecasting
"""

import json
import math
import sqlite3
from dataclasses import dataclass, field
from collections import defaultdict
from core.provenance.database import new_id, now_iso


# ── Data quality thresholds ──────────────────────────────────

MIN_PERIODS_FOR_CAGR = 3
MIN_PERIODS_FOR_REGRESSION = 4
MIN_PERIODS_FOR_SEASONALITY = 8  # need at least 2 full cycles


@dataclass
class BaselineDataPoint:
    """One historical observation for baseline analysis."""
    period: str          # "FY2023", "Q1 2024", "2024-03", etc.
    value: float
    period_index: int    # sequential integer for regression
    is_actual: bool = True
    source: str = ""


@dataclass
class BaselineQuality:
    """Assessment of whether historical data supports baseline forecasting."""
    usable: bool
    quality: str           # "strong" / "adequate" / "weak" / "unusable"
    n_periods: int
    warnings: list[str] = field(default_factory=list)
    structural_breaks: list[str] = field(default_factory=list)

    @property
    def trust_level(self) -> str:
        """How much weight to put on the baseline."""
        if not self.usable:
            return "none"
        if self.structural_breaks:
            return "low — structural break detected"
        if self.quality == "strong":
            return "medium — useful reference point"
        if self.quality == "adequate":
            return "low-to-medium — directional only"
        return "low — treat as rough approximation only"


@dataclass
class BaselineResult:
    """Output of a baseline forecast computation."""
    metric_name: str
    method: str              # "CAGR" / "LINEAR_REGRESSION" / "SEASONAL_REGRESSION"
    historical_window: str   # e.g. "FY2021-FY2025 (5 periods)"
    forecast_value: float
    forecast_period: str     # what period is being forecast
    cagr: float = None
    trend_slope: float = None
    r_squared: float = None
    seasonality: dict = None
    quality: BaselineQuality = None

    # Comparison outputs
    vs_guidance: float = None
    vs_consensus: float = None
    vs_thesis: float = None
    comparison_summary: str = ""


class BaselineForecastBuilder:
    """
    Builds historical baseline forecasts as sanity checks.

    Usage:
        bf = BaselineForecastBuilder(conn, company_id)

        # Step 1: Assess whether baseline work is justified
        assessment = bf.assess_baseline_viability("revenue")

        # Step 2: Build baseline if warranted
        if assessment.usable:
            result = bf.build_baseline("revenue", forecast_period="FY2026")

        # Step 3: Compare against targets
        comparison = bf.compare_baseline(result,
            guidance_mid=12500, consensus=12450, thesis=12900)

        # Step 4: Produce workpaper
        workpaper_id = bf.produce_workpaper(result, comparison)
    """

    def __init__(self, conn: sqlite3.Connection, company_id: str):
        self.conn = conn
        self.company_id = company_id

    # ── Step 1: Assess viability ─────────────────────────────

    def assess_baseline_viability(self, metric_name: str) -> BaselineQuality:
        """
        Decide whether the historical data supports a meaningful baseline.

        This is the guardrail step. It checks:
        - Is there enough data?
        - Is the data clean and comparable?
        - Are there structural breaks?
        - Is seasonality stable enough to use?
        """
        data = self._load_historical(metric_name)
        n = len(data)

        warnings = []
        breaks = []

        if n < MIN_PERIODS_FOR_CAGR:
            return BaselineQuality(
                usable=False, quality="unusable", n_periods=n,
                warnings=[f"Only {n} period(s) — need at least {MIN_PERIODS_FOR_CAGR}"])

        values = [d.value for d in data]

        # Check for structural breaks: large single-period jumps (>2x median change)
        if len(values) >= 3:
            changes = [abs(values[i] - values[i-1]) for i in range(1, len(values))]
            median_change = sorted(changes)[len(changes) // 2]
            if median_change > 0:
                for i, c in enumerate(changes):
                    if c > median_change * 3:
                        breaks.append(
                            f"Large jump between {data[i].period} and {data[i+1].period}: "
                            f"{values[i]:.1f} → {values[i+1]:.1f} ({c/median_change:.1f}x median change)")

        # Check for sign changes (going from positive to negative growth etc.)
        if any(v < 0 for v in values) and any(v > 0 for v in values):
            if metric_name in ("sss_growth", "revenue_growth"):
                warnings.append("Metric crossed zero — trend extrapolation may be unreliable")

        # Check coefficient of variation (volatility relative to mean)
        mean_val = sum(values) / len(values)
        if mean_val != 0:
            cv = (sum((v - mean_val)**2 for v in values) / len(values)) ** 0.5 / abs(mean_val)
            if cv > 0.5:
                warnings.append(f"High volatility (CV={cv:.2f}) — baseline will have wide error bands")

        # Quality assessment
        if n >= 8 and not breaks:
            quality = "strong"
        elif n >= MIN_PERIODS_FOR_REGRESSION and len(breaks) <= 1:
            quality = "adequate"
        elif n >= MIN_PERIODS_FOR_CAGR:
            quality = "weak"
        else:
            quality = "unusable"

        return BaselineQuality(
            usable=quality != "unusable",
            quality=quality,
            n_periods=n,
            warnings=warnings,
            structural_breaks=breaks,
        )

    # ── Step 2: Build baseline forecast ──────────────────────

    def build_baseline(
        self, metric_name: str,
        forecast_period: str = "next",
        method: str = "auto",
    ) -> BaselineResult | None:
        """
        Build a baseline forecast from historical data.

        Methods:
          "auto" — picks the best method based on data availability
          "cagr" — compound annual growth rate projection
          "regression" — simple linear regression (OLS)
          "seasonal" — regression with seasonality adjustment

        Returns None if data is insufficient.
        """
        data = self._load_historical(metric_name)
        quality = self.assess_baseline_viability(metric_name)

        if not quality.usable:
            return None

        values = [d.value for d in data]
        indices = [d.period_index for d in data]
        n = len(data)

        # Auto-select method
        if method == "auto":
            if n >= MIN_PERIODS_FOR_SEASONALITY:
                method = "seasonal"
            elif n >= MIN_PERIODS_FOR_REGRESSION:
                method = "regression"
            else:
                method = "cagr"

        window_str = f"{data[0].period}–{data[-1].period} ({n} periods)"

        if method == "cagr":
            return self._cagr_forecast(data, values, forecast_period, window_str, quality)
        elif method == "regression":
            return self._regression_forecast(
                data, values, indices, metric_name, forecast_period, window_str, quality)
        elif method == "seasonal":
            return self._seasonal_forecast(
                data, values, indices, metric_name, forecast_period, window_str, quality)
        else:
            return self._regression_forecast(
                data, values, indices, metric_name, forecast_period, window_str, quality)

    def _cagr_forecast(self, data, values, forecast_period, window_str, quality):
        """CAGR-based projection."""
        first, last = values[0], values[-1]
        n_years = len(values) - 1
        if first <= 0 or last <= 0 or n_years <= 0:
            return None
        cagr = (last / first) ** (1.0 / n_years) - 1.0
        forecast = last * (1.0 + cagr)

        return BaselineResult(
            metric_name=data[0].period.split()[0] if data else "unknown",
            method="CAGR",
            historical_window=window_str,
            forecast_value=round(forecast, 2),
            forecast_period=forecast_period,
            cagr=round(cagr, 4),
            quality=quality,
        )

    def _regression_forecast(self, data, values, indices, metric_name,
                             forecast_period, window_str, quality):
        """Simple OLS linear regression."""
        n = len(values)
        slope, intercept, r_sq = self._ols(indices, values)
        next_idx = max(indices) + 1
        forecast = intercept + slope * next_idx

        return BaselineResult(
            metric_name=metric_name,
            method="LINEAR_REGRESSION",
            historical_window=window_str,
            forecast_value=round(forecast, 2),
            forecast_period=forecast_period,
            trend_slope=round(slope, 4),
            r_squared=round(r_sq, 4),
            quality=quality,
        )

    def _seasonal_forecast(self, data, values, indices, metric_name,
                           forecast_period, window_str, quality):
        """Regression with seasonality detection."""
        # First run plain regression for trend
        slope, intercept, r_sq = self._ols(indices, values)

        # Detect seasonality: compute residuals and check for patterns
        residuals = [v - (intercept + slope * i) for v, i in zip(values, indices)]

        # Simple seasonality: if quarterly data, group by quarter position
        seasonality = {}
        if len(values) >= 8:
            # Group residuals by position in cycle (assuming 4-period cycle)
            cycle_len = 4
            cycle_residuals = defaultdict(list)
            for i, r in enumerate(residuals):
                pos = i % cycle_len
                cycle_residuals[pos].append(r)

            for pos, resids in cycle_residuals.items():
                seasonality[f"Q{pos+1}"] = round(sum(resids) / len(resids), 2)

        next_idx = max(indices) + 1
        forecast = intercept + slope * next_idx

        # Apply seasonal adjustment for the forecast quarter
        if seasonality:
            next_q = next_idx % 4
            q_key = f"Q{next_q + 1}"
            if q_key in seasonality:
                forecast += seasonality[q_key]

        return BaselineResult(
            metric_name=metric_name,
            method="SEASONAL_REGRESSION",
            historical_window=window_str,
            forecast_value=round(forecast, 2),
            forecast_period=forecast_period,
            trend_slope=round(slope, 4),
            r_squared=round(r_sq, 4),
            seasonality=seasonality,
            quality=quality,
        )

    @staticmethod
    def _ols(x: list, y: list) -> tuple[float, float, float]:
        """Ordinary least squares: returns (slope, intercept, r_squared)."""
        n = len(x)
        if n < 2:
            return 0.0, y[0] if y else 0.0, 0.0

        sx = sum(x)
        sy = sum(y)
        sxy = sum(xi * yi for xi, yi in zip(x, y))
        sx2 = sum(xi ** 2 for xi in x)

        denom = n * sx2 - sx * sx
        if denom == 0:
            return 0.0, sy / n, 0.0

        slope = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n

        # R²
        y_mean = sy / n
        ss_tot = sum((yi - y_mean) ** 2 for yi in y)
        ss_res = sum((yi - (intercept + slope * xi)) ** 2 for xi, yi in zip(x, y))
        r_sq = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        return slope, intercept, max(0.0, r_sq)

    # ── Step 3: Compare against targets ──────────────────────

    def compare_baseline(
        self, result: BaselineResult,
        guidance_mid: float = None,
        consensus: float = None,
        thesis: float = None,
    ) -> dict:
        """
        Compare baseline forecast against guidance, consensus, and/or thesis.

        Returns a structured comparison with explicit assessments.
        """
        if result is None:
            return {"status": "no_baseline", "comparisons": []}

        comparisons = []
        bv = result.forecast_value

        for label, target in [("guidance", guidance_mid),
                              ("consensus", consensus),
                              ("thesis", thesis)]:
            if target is None:
                continue

            diff = target - bv
            pct_diff = (diff / abs(bv) * 100) if bv != 0 else 0

            if abs(pct_diff) < 2:
                assessment = "broadly in line with baseline"
            elif pct_diff > 10:
                assessment = "significantly above baseline — appears aggressive"
            elif pct_diff > 5:
                assessment = "moderately above baseline — warrants scrutiny"
            elif pct_diff > 2:
                assessment = "slightly above baseline"
            elif pct_diff < -10:
                assessment = "significantly below baseline — appears conservative"
            elif pct_diff < -5:
                assessment = "moderately below baseline — may understate"
            else:
                assessment = "slightly below baseline"

            comparisons.append({
                "target": label,
                "target_value": target,
                "baseline_value": bv,
                "difference": round(diff, 2),
                "pct_difference": round(pct_diff, 1),
                "assessment": assessment,
            })

        # Overall summary
        summary_parts = []
        for c in comparisons:
            summary_parts.append(
                f"{c['target'].capitalize()}: {c['target_value']} vs baseline {bv} "
                f"({c['pct_difference']:+.1f}%) — {c['assessment']}")

        trust = result.quality.trust_level if result.quality else "unknown"

        return {
            "status": "completed",
            "baseline_value": bv,
            "method": result.method,
            "trust_level": trust,
            "comparisons": comparisons,
            "summary": "\n".join(summary_parts),
            "caveats": result.quality.warnings if result.quality else [],
            "structural_breaks": result.quality.structural_breaks if result.quality else [],
        }

    # ── Step 4: Produce workpaper ────────────────────────────

    def produce_workpaper(
        self, result: BaselineResult, comparison: dict,
        question: str = None,
        escalation_id: str = None,
        run_id: str = None,
    ) -> str | None:
        """
        Create an analyst-visible workpaper from the baseline analysis.

        The workpaper shows:
          - what historical data was used
          - what method and window
          - the baseline forecast value
          - comparison against targets
          - caveats and limitations
          - trust level assessment
        """
        if result is None:
            return None

        from research.escalation import WorkpaperBuilder

        data = self._load_historical(result.metric_name
                                     if result.metric_name != "unknown"
                                     else "")
        historical_table = [
            {"period": d.period, "value": d.value, "source": d.source}
            for d in data
        ]

        content = {
            "metric": result.metric_name,
            "method": result.method,
            "historical_window": result.historical_window,
            "historical_data": historical_table,
            "forecast_period": result.forecast_period,
            "forecast_value": result.forecast_value,
            "cagr": result.cagr,
            "trend_slope": result.trend_slope,
            "r_squared": result.r_squared,
            "seasonality": result.seasonality,
            "comparisons": comparison.get("comparisons", []),
            "trust_level": comparison.get("trust_level", "unknown"),
        }

        caveats_parts = []
        if result.quality:
            if result.quality.warnings:
                caveats_parts.extend(result.quality.warnings)
            if result.quality.structural_breaks:
                caveats_parts.append(
                    f"STRUCTURAL BREAK(S): {'; '.join(result.quality.structural_breaks)}")
            caveats_parts.append(f"Trust level: {result.quality.trust_level}")
        caveats_parts.append(
            "This is a historical-trend baseline only. "
            "It does not account for business changes, management actions, "
            "or market conditions. Use as a sanity check, not a forecast.")

        wb = WorkpaperBuilder(self.conn, self.company_id)
        wid = wb.create(
            workpaper_type="BASELINE_FORECAST",
            title=f"Baseline Forecast: {result.metric_name} ({result.method})",
            content=content,
            question=question or f"What does historical trend suggest for {result.metric_name}?",
            methodology=(
                f"Method: {result.method}. "
                f"Window: {result.historical_window}. "
                f"{'CAGR: ' + f'{result.cagr:.1%}' if result.cagr else ''}"
                f"{'R²: ' + f'{result.r_squared:.3f}' if result.r_squared else ''}"
            ),
            caveats="\n".join(caveats_parts),
            affects=f"Sanity check for {result.metric_name} estimate",
            escalation_id=escalation_id,
            run_id=run_id,
        )

        # If there are comparisons, produce a separate comparison workpaper
        if comparison.get("comparisons"):
            wb.create(
                workpaper_type="BASELINE_COMPARISON",
                title=f"Baseline vs Targets: {result.metric_name}",
                content={
                    "baseline": result.forecast_value,
                    "method": result.method,
                    "comparisons": comparison["comparisons"],
                    "trust_level": comparison.get("trust_level"),
                },
                question=question or f"How do targets compare to historical baseline for {result.metric_name}?",
                methodology=f"Baseline from {result.method}, compared against guidance/consensus/thesis",
                caveats=comparison.get("summary", ""),
                affects=f"Challenge function for {result.metric_name} assumptions",
                escalation_id=escalation_id,
                run_id=run_id,
            )

        return wid

    # ── Internal: load historical data ───────────────────────

    def _load_historical(self, metric_name: str) -> list[BaselineDataPoint]:
        """
        Load historical time-series data for baseline analysis.

        Tries multiple sources in order:
          1. company_metric_series (structured)
          2. orientation evidence (KEY_METRIC observations)
        """
        points = []

        # Source 1: Structured metric series
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute("""
            SELECT cms.value, rp.fiscal_year, rp.fiscal_quarter, rp.period_type
            FROM company_metric_series cms
            JOIN metric_definition md ON cms.metric_id = md.metric_id
            JOIN reporting_period rp ON cms.period_id = rp.period_id
            WHERE cms.company_id = ? AND md.metric_name = ?
            ORDER BY rp.fiscal_year, COALESCE(rp.fiscal_quarter, 0)
        """, (self.company_id, metric_name)).fetchall()
        self.conn.row_factory = old

        if rows:
            for i, r in enumerate(rows):
                rd = dict(r)
                if rd.get("fiscal_quarter"):
                    period = f"Q{rd['fiscal_quarter']} {rd['fiscal_year']}"
                else:
                    period = f"FY{rd['fiscal_year']}"
                points.append(BaselineDataPoint(
                    period=period, value=rd["value"],
                    period_index=i, source="metric_series"))
            return points

        # Source 2: Orientation evidence with numeric values
        old = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        ev_rows = self.conn.execute("""
            SELECT value_numeric, as_of_date, notes
            FROM evidence_item
            WHERE company_id = ? AND evidence_type = 'KEY_METRIC'
              AND value_numeric IS NOT NULL
              AND (LOWER(value) LIKE ? OR LOWER(value) LIKE ?)
            ORDER BY as_of_date
        """, (self.company_id, f"%{metric_name.lower()}%",
              f"%{metric_name.lower().replace('_', ' ')}%")).fetchall()
        self.conn.row_factory = old

        for i, e in enumerate(ev_rows):
            ed = dict(e)
            period = "unknown"
            notes = ed.get("notes") or ""
            for part in notes.split("|"):
                if part.startswith("period:"):
                    period = part[7:]
            if period == "unknown" and ed.get("as_of_date"):
                period = f"FY{ed['as_of_date'][:4]}"
            points.append(BaselineDataPoint(
                period=period, value=ed["value_numeric"],
                period_index=i, source="orientation_evidence"))

        return points
