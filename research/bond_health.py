"""
Bond Health — Issuer Credit Monitor

Orchestrates the bond pipeline for one issuer:
  1. Pull bond universe from EDGAR (debt schedule loader)
  2. Persist universe to bond_universe table
  3. For each bond with a CUSIP, fetch TRACE prices (or manual CSV)
  4. Persist daily prices, compute YTW + G-spread per day
  5. Persist daily spread snapshots
  6. Compute trailing 30d / 90d spread changes per bond
  7. Compute trailing 6m stdev — flag any bond whose 30d move > 1 stdev
  8. Pull equity history (yfinance) — compute 30d / 90d % change
  9. Issuer-level: avg spread, weighted by par; credit-equity divergence
     (avg spread widening AND equity flat or up over the same window)
 10. Persist issuer_credit_snapshot
 11. Render corpus text for brief injection

The system is designed to be silent-but-useful when prices are missing:
  * No CUSIPs?    Bond universe still rendered with maturity ladder.
  * No FINRA creds? "Spread monitor inactive — register at gateway.finra.org"
  * Insufficient history? Stats computed only over what exists; flags
    are conservative (require ≥30 days of history for the 1-stdev flag).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from core.provenance.database import init_db, new_id, now_iso, RunContext
from ingestion.loaders.edgar_debt_schedule_loader import (
    fetch_debt_schedule, BondSeries,
)
from ingestion.loaders.finra_trace_loader import (
    fetch_bond_prices, BondPriceObservation,
)
from ingestion.loaders.finra_market_credit_loader import (
    fetch_market_credit_sentiment,
)
from ingestion.loaders.treasury_curve_loader import (
    fetch_treasury_curve, TreasuryCurve,
)
from research.bond_spreads import bond_yields, g_spread_bps


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Manual mapping file for series_label -> CUSIP. Format:
# {
#   "<issuer_cik>": {
#     "<series_label>": "<CUSIP>"
#   }
# }
_CUSIP_OVERRIDES_PATH = Path("data/bond_cusip_overrides.json")


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass
class BondHealthFinding:
    bond_id: str = ""
    series_label: str = ""
    coupon_pct: float | None = None
    par_amount_m: float | None = None
    maturity_date: str = ""
    years_to_maturity: float | None = None
    is_callable: bool = False
    cusip: str = ""

    # Pricing status
    has_price: bool = False
    last_price: float | None = None
    last_price_date: str = ""
    price_source: str = ""

    # Today's spread snapshot
    ytw_pct: float | None = None
    ytw_horizon_years: float | None = None
    g_spread_bps: float | None = None

    # Trend stats vs 30d / 90d ago
    spread_30d_chg_bps: float | None = None
    spread_90d_chg_bps: float | None = None
    spread_stdev_6m_bps: float | None = None
    n_snapshots_6m: int = 0

    # Flags
    widen_1stdev_flag: bool = False


@dataclass
class BondHealthBundle:
    ticker: str = ""
    issuer_cik: str = ""
    issuer_name: str = ""
    fetched_at: str = ""
    snapshot_date: str = ""

    bond_count: int = 0
    n_priced: int = 0
    total_long_term_debt_m: float | None = None
    other_long_term_debt_m: float | None = None
    most_recent_10k_filed_date: str = ""

    findings: list = field(default_factory=list)  # list[BondHealthFinding]

    # Issuer-level aggregate (par-weighted)
    avg_g_spread_bps: float | None = None
    avg_spread_30d_chg_bps: float | None = None
    avg_spread_90d_chg_bps: float | None = None
    n_widening_1stdev: int = 0

    # Equity context
    equity_close: float | None = None
    equity_30d_chg_pct: float | None = None
    equity_90d_chg_pct: float | None = None
    credit_equity_divergence_flag: bool = False

    # Market-level credit context (from FINRA CORPORATEMARKETSENTIMENT —
    # works on the free FINRA Data Gateway tier; no per-CUSIP needed).
    market_credit_text: str = ""
    market_regime: str = ""              # 'RISK-ON'|'RISK-OFF'|'DEFENSIVE'|...
    ig_net_flow_today_m: float | None = None
    ig_net_flow_30d_avg_m: float | None = None
    hy_net_flow_today_m: float | None = None
    hy_net_flow_30d_avg_m: float | None = None
    hy_ig_volume_ratio: float | None = None

    # Status
    auth_status: str = ""    # 'ok'|'no_credentials'|'manual_csv'|'no_bonds'|'auth_failed'
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "issuer_cik": self.issuer_cik,
            "issuer_name": self.issuer_name,
            "fetched_at": self.fetched_at,
            "snapshot_date": self.snapshot_date,
            "bond_count": self.bond_count,
            "n_priced": self.n_priced,
            "total_long_term_debt_m": self.total_long_term_debt_m,
            "other_long_term_debt_m": self.other_long_term_debt_m,
            "most_recent_10k_filed_date": self.most_recent_10k_filed_date,
            "findings": [asdict(f) for f in self.findings],
            "avg_g_spread_bps": self.avg_g_spread_bps,
            "avg_spread_30d_chg_bps": self.avg_spread_30d_chg_bps,
            "avg_spread_90d_chg_bps": self.avg_spread_90d_chg_bps,
            "n_widening_1stdev": self.n_widening_1stdev,
            "equity_close": self.equity_close,
            "equity_30d_chg_pct": self.equity_30d_chg_pct,
            "equity_90d_chg_pct": self.equity_90d_chg_pct,
            "credit_equity_divergence_flag": self.credit_equity_divergence_flag,
            "auth_status": self.auth_status,
            "error": self.error,
        }

    def to_prompt_text(self) -> str:
        # Always include the macro-credit panel if we have it (works
        # without bonds — useful for any equity research run as macro
        # context). Suppress only if we have neither bonds nor sentiment.
        if (self.bond_count == 0 and not self.total_long_term_debt_m
                and not self.market_credit_text):
            return ""

        sections: list[str] = []
        if self.market_credit_text:
            sections.append(self.market_credit_text)

        if self.bond_count == 0 and not self.total_long_term_debt_m:
            return "\n\n".join(sections)

        lines = [
            f"=== ISSUER CREDIT / BOND HEALTH ({self.ticker}, snapshot "
            f"{self.snapshot_date}) ===",
        ]
        if self.most_recent_10k_filed_date:
            lines.append(
                f"(Bond universe sourced from 10-K filed "
                f"{self.most_recent_10k_filed_date}. Spreads are G-spread "
                f"approximation: yield-to-worst minus interpolated UST par "
                f"yield at matching horizon.)"
            )
        else:
            lines.append("(Bond universe + maturity ladder. No public bond "
                          "schedule found in latest 10-K.)")
        lines.append("")

        # Bond ladder + spread per bond
        if self.findings:
            sorted_bonds = sorted(
                self.findings,
                key=lambda f: (f.maturity_date or "9999"),
            )
            for f in sorted_bonds:
                par = f"${f.par_amount_m:,.0f}M" if f.par_amount_m else "?"
                cpn = f"{f.coupon_pct:.3f}%" if f.coupon_pct is not None else "?"
                mat = f.maturity_date or "?"
                call_tag = "[CALLABLE] " if f.is_callable else ""
                lines.append(f"  {call_tag}{f.series_label}")
                lines.append(f"      Par: {par} | Coupon: {cpn} | Maturity: {mat}")
                if f.has_price and f.g_spread_bps is not None:
                    bits = [
                        f"price ${f.last_price:.2f}" if f.last_price else "",
                        f"YTW {f.ytw_pct:.2f}%" if f.ytw_pct else "",
                        f"G-spread {f.g_spread_bps:.0f}bps",
                    ]
                    lines.append(f"      Spread: {' / '.join(b for b in bits if b)} "
                                  f"({f.last_price_date})")
                    trend_bits = []
                    if f.spread_30d_chg_bps is not None:
                        sign = "+" if f.spread_30d_chg_bps >= 0 else ""
                        trend_bits.append(f"30d {sign}{f.spread_30d_chg_bps:.0f}bps")
                    if f.spread_90d_chg_bps is not None:
                        sign = "+" if f.spread_90d_chg_bps >= 0 else ""
                        trend_bits.append(f"90d {sign}{f.spread_90d_chg_bps:.0f}bps")
                    if trend_bits:
                        flag = " ⚠ >1σ WIDENING" if f.widen_1stdev_flag else ""
                        lines.append(f"      Trend: {' / '.join(trend_bits)}{flag}")
                else:
                    if f.price_source == "no_cusip":
                        lines.append(f"      Spread: no CUSIP mapped — "
                                      f"add to data/bond_cusip_overrides.json")
                    elif f.price_source in ("no_credentials", "auth_failed"):
                        lines.append(f"      Spread: FINRA OAuth unavailable")
                    elif f.price_source == "no_data":
                        lines.append(f"      Spread: per-CUSIP price feed not "
                                      f"available on free tier (need paid "
                                      f"TRACE subscription or "
                                      f"data/manual_bond_prices.csv)")
                    else:
                        lines.append(f"      Spread: no price data this run")
            lines.append("")

        # Issuer-level aggregate
        if self.avg_g_spread_bps is not None:
            lines.append(
                f"Issuer avg G-spread (par-weighted): "
                f"{self.avg_g_spread_bps:.0f}bps"
            )
            agg_bits = []
            if self.avg_spread_30d_chg_bps is not None:
                sign = "+" if self.avg_spread_30d_chg_bps >= 0 else ""
                agg_bits.append(f"30d {sign}{self.avg_spread_30d_chg_bps:.0f}bps")
            if self.avg_spread_90d_chg_bps is not None:
                sign = "+" if self.avg_spread_90d_chg_bps >= 0 else ""
                agg_bits.append(f"90d {sign}{self.avg_spread_90d_chg_bps:.0f}bps")
            if agg_bits:
                lines.append(f"Issuer-level change: {' / '.join(agg_bits)}")
            if self.n_widening_1stdev > 0:
                lines.append(f"⚠ {self.n_widening_1stdev} bond(s) widening "
                              f">1σ vs 6m baseline")
            if self.credit_equity_divergence_flag:
                eq30 = (f"+{self.equity_30d_chg_pct:.1f}%"
                        if self.equity_30d_chg_pct >= 0
                        else f"{self.equity_30d_chg_pct:.1f}%")
                lines.append(
                    f"⚠ CREDIT-EQUITY DIVERGENCE: avg spread widened "
                    f"{self.avg_spread_30d_chg_bps:+.0f}bps over 30d while "
                    f"equity {eq30} — historically a leading risk signal"
                )
            lines.append("")

        # Status footer
        if self.auth_status == "no_credentials":
            lines.append("(No FINRA OAuth creds set. Add "
                          "FINRA_DATA_CLIENT_ID / FINRA_DATA_CLIENT_SECRET "
                          "to .env to enable the macro-credit panel.)")
        elif self.auth_status == "auth_failed":
            lines.append("(FINRA OAuth failed — re-check creds in .env)")
        elif self.auth_status == "no_per_cusip":
            lines.append("(Per-bond spread monitor: free FINRA tier exposes "
                          "market-level aggregates only (panel above), not "
                          "per-CUSIP TRACE prices. Activate per-bond by "
                          "either (a) a paid FINRA TRACE subscription, or "
                          "(b) dropping manual prices into "
                          "data/manual_bond_prices.csv.)")
        lines.append(f"Fetched: {self.fetched_at}")
        lines.append("=" * 60)
        sections.append("\n".join(lines))
        return "\n\n".join(sections)


# --------------------------------------------------------------------------
# CUSIP override loading
# --------------------------------------------------------------------------

def _load_cusip_overrides(issuer_cik: str) -> dict[str, str]:
    """Return {series_label: cusip} from the override file for this issuer."""
    if not _CUSIP_OVERRIDES_PATH.exists():
        return {}
    try:
        d = json.loads(_CUSIP_OVERRIDES_PATH.read_text(encoding="utf-8"))
        return d.get(issuer_cik, {}) or d.get(issuer_cik.lstrip("0"), {})
    except Exception:
        return {}


# --------------------------------------------------------------------------
# DB persistence
# --------------------------------------------------------------------------

def _upsert_bond_universe(
    db: sqlite3.Connection,
    issuer_cik: str,
    issuer_ticker: str,
    issuer_name: str,
    bonds: list,
    cusip_map: dict,
    last_seen_filing: str,
    last_seen_date: str,
    run_id: str,
) -> dict[str, str]:
    """Upsert each bond series. Returns {series_label: bond_id}."""
    label_to_id: dict[str, str] = {}
    for b in bonds:
        cusip = cusip_map.get(b.series_label, "") or ""
        # Try to find existing row
        row = db.execute(
            "SELECT bond_id FROM bond_universe WHERE issuer_cik = ? AND series_label = ?",
            (issuer_cik, b.series_label),
        ).fetchone()
        if row:
            bond_id = row[0]
            db.execute(
                """UPDATE bond_universe SET
                       coupon_pct = ?, par_amount_m = ?, maturity_date = ?,
                       cusip = COALESCE(NULLIF(?, ''), cusip),
                       is_callable = ?, call_type = ?, redemption_terms = ?,
                       last_seen_filing = ?, last_seen_date = ?, is_active = 1,
                       run_id = ?
                   WHERE bond_id = ?""",
                (
                    b.coupon_pct, b.par_amount_m, b.maturity_date,
                    cusip,
                    1 if b.is_callable else 0, b.call_type, b.redemption_terms,
                    last_seen_filing, last_seen_date, run_id, bond_id,
                ),
            )
        else:
            bond_id = new_id()
            db.execute(
                """INSERT INTO bond_universe
                   (bond_id, issuer_cik, issuer_ticker, issuer_name, cusip,
                    series_label, coupon_pct, par_amount_m, maturity_date,
                    is_callable, call_type, redemption_terms,
                    last_seen_filing, last_seen_date, is_active, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    bond_id, issuer_cik, issuer_ticker, issuer_name, cusip,
                    b.series_label, b.coupon_pct, b.par_amount_m, b.maturity_date,
                    1 if b.is_callable else 0, b.call_type, b.redemption_terms,
                    last_seen_filing, last_seen_date, run_id,
                ),
            )
        label_to_id[b.series_label] = bond_id
    db.commit()
    return label_to_id


def _upsert_bond_prices(
    db: sqlite3.Connection,
    bond_id: str,
    cusip: str,
    observations: list,
    run_id: str,
) -> int:
    """Upsert daily price observations. Returns count of rows touched."""
    n = 0
    for obs in observations:
        if not obs.trade_date or obs.price is None:
            continue
        existing = db.execute(
            "SELECT price_id FROM bond_price WHERE bond_id = ? AND trade_date = ?",
            (bond_id, obs.trade_date),
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE bond_price SET price = ?, yield_pct = ?, volume = ?,
                       n_trades = ?, source = ?, run_id = ?
                   WHERE price_id = ?""",
                (obs.price, obs.yield_pct, obs.volume, obs.n_trades,
                 obs.source, run_id, existing[0]),
            )
        else:
            db.execute(
                """INSERT INTO bond_price (price_id, bond_id, cusip, trade_date,
                       price, yield_pct, volume, n_trades, source, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id(), bond_id, cusip, obs.trade_date, obs.price,
                 obs.yield_pct, obs.volume, obs.n_trades, obs.source, run_id),
            )
            n += 1
    db.commit()
    return n


def _upsert_spread_snapshot(
    db: sqlite3.Connection,
    bond_id: str,
    snapshot_date: str,
    price: float,
    ytm_pct: float | None,
    ytw_pct: float | None,
    spread_bps: float | None,
    treasury_yield: float | None,
    benchmark_tenor: float | None,
    run_id: str,
) -> None:
    existing = db.execute(
        "SELECT snapshot_id FROM bond_spread_snapshot WHERE bond_id = ? AND snapshot_date = ?",
        (bond_id, snapshot_date),
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE bond_spread_snapshot SET price = ?, ytm_pct = ?, ytw_pct = ?,
                   z_spread_bps = ?, treasury_benchmark_yield = ?,
                   benchmark_tenor_years = ?, run_id = ?
               WHERE snapshot_id = ?""",
            (price, ytm_pct, ytw_pct, spread_bps, treasury_yield,
             benchmark_tenor, run_id, existing[0]),
        )
    else:
        db.execute(
            """INSERT INTO bond_spread_snapshot
               (snapshot_id, bond_id, snapshot_date, price, ytm_pct, ytw_pct,
                z_spread_bps, treasury_benchmark_yield, benchmark_tenor_years,
                run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id(), bond_id, snapshot_date, price, ytm_pct, ytw_pct,
             spread_bps, treasury_yield, benchmark_tenor, run_id),
        )


# --------------------------------------------------------------------------
# Trend / stdev queries
# --------------------------------------------------------------------------

def _get_historical_spreads(
    db: sqlite3.Connection,
    bond_id: str,
    *,
    days: int,
) -> list[tuple[str, float]]:
    """Return [(snapshot_date, z_spread_bps)] sorted ascending for the
    trailing `days` window."""
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    rows = db.execute(
        """SELECT snapshot_date, z_spread_bps
           FROM bond_spread_snapshot
           WHERE bond_id = ? AND snapshot_date >= ? AND z_spread_bps IS NOT NULL
           ORDER BY snapshot_date ASC""",
        (bond_id, cutoff),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _spread_change(
    history: list[tuple[str, float]],
    *,
    target_lookback_days: int,
    snapshot_date: str,
) -> float | None:
    """Return (today's spread - spread N days ago) in bps. None if
    insufficient history."""
    if not history:
        return None
    today_spread = history[-1][1]
    target = (datetime.fromisoformat(snapshot_date).date()
              - timedelta(days=target_lookback_days)).isoformat()
    # Walk back through history, find the latest entry on or before target
    prior = None
    for d, s in history:
        if d <= target:
            prior = s
        else:
            break
    if prior is None:
        return None
    return today_spread - prior


def _spread_stdev(history: list[tuple[str, float]]) -> tuple[float | None, int]:
    """Trailing window stdev of spread observations + count."""
    vals = [s for _, s in history]
    n = len(vals)
    if n < 2:
        return None, n
    return statistics.stdev(vals), n


# --------------------------------------------------------------------------
# Equity history (for credit-equity divergence)
# --------------------------------------------------------------------------

def _fetch_equity_history(ticker: str, *, verbose: bool = False) -> dict:
    """Pull last 6 months of equity closes via yfinance. Returns
    {today_close, chg_30d_pct, chg_90d_pct} (None on failure)."""
    out = {"today_close": None, "chg_30d_pct": None, "chg_90d_pct": None}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="6mo", auto_adjust=False)
        if hist is None or hist.empty:
            return out
        closes = hist["Close"].dropna()
        if closes.empty:
            return out
        out["today_close"] = float(closes.iloc[-1])
        # 30d / 90d ago — find closest trading day at least N calendar days back
        today_ts = closes.index[-1]
        for label, days in (("chg_30d_pct", 30), ("chg_90d_pct", 90)):
            target = today_ts - timedelta(days=days)
            prior = closes[closes.index <= target]
            if not prior.empty:
                p_old = float(prior.iloc[-1])
                if p_old > 0:
                    out[label] = (out["today_close"] - p_old) / p_old * 100.0
    except Exception as e:
        if verbose:
            print(f"  Equity history: {type(e).__name__}: {e}")
    return out


# --------------------------------------------------------------------------
# Top-level orchestrator
# --------------------------------------------------------------------------

def assess_bond_health(
    ticker: str,
    *,
    snapshot_date: str | None = None,
    persist: bool = True,
    verbose: bool = False,
) -> BondHealthBundle:
    """End-to-end: pull bonds, prices, treasury curve; compute spreads,
    trend stats, divergence flag; persist; render corpus block."""
    snapshot_date = snapshot_date or datetime.now().date().isoformat()
    bundle = BondHealthBundle(
        ticker=ticker.upper(),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        snapshot_date=snapshot_date,
    )

    # 0. Market-level credit context (FINRA CORPORATEMARKETSENTIMENT) —
    # works on the free Gateway tier and gives a macro risk-on/off read
    # that's useful even when per-issuer pricing is unavailable.
    try:
        mc = fetch_market_credit_sentiment(lookback_days=60, verbose=verbose)
        if mc.days:
            bundle.market_credit_text = mc.to_prompt_text()
            latest = mc.latest
            bundle.ig_net_flow_today_m = latest.ig_net_flow
            bundle.hy_net_flow_today_m = latest.hy_net_flow
            bundle.hy_ig_volume_ratio = latest.hy_ig_volume_ratio
            bundle.ig_net_flow_30d_avg_m = mc.trailing_avg_net(
                days=30, attr="ig_net_flow")
            bundle.hy_net_flow_30d_avg_m = mc.trailing_avg_net(
                days=30, attr="hy_net_flow")
            # Pull the regime label out of the rendered text (cheap parse)
            for ln in bundle.market_credit_text.splitlines():
                if ln.strip().startswith("CREDIT REGIME:"):
                    bundle.market_regime = ln.split(":", 1)[1].strip()
                    break
    except Exception as e:
        if verbose:
            print(f"  Market credit fetch failed: {type(e).__name__}: {e}")

    # 1. Bond universe
    debt = fetch_debt_schedule(ticker, verbose=verbose)
    bundle.issuer_cik = debt.issuer_cik
    bundle.issuer_name = debt.issuer_name
    bundle.most_recent_10k_filed_date = debt.most_recent_10k_filed_date
    bundle.bond_count = len(debt.bonds)
    bundle.total_long_term_debt_m = debt.total_long_term_debt_m
    bundle.other_long_term_debt_m = debt.other_long_term_debt_m

    if not debt.bonds:
        bundle.auth_status = "no_bonds"
        if debt.error:
            bundle.error = debt.error
        return bundle

    # 2. CUSIP overrides
    cusip_map = _load_cusip_overrides(debt.issuer_cik)

    # 3. Treasury curve
    curve = fetch_treasury_curve(verbose=verbose)

    # 4. Equity context
    eq = _fetch_equity_history(ticker, verbose=verbose)
    bundle.equity_close = eq["today_close"]
    bundle.equity_30d_chg_pct = eq["chg_30d_pct"]
    bundle.equity_90d_chg_pct = eq["chg_90d_pct"]

    # 5. Per-bond loop with optional DB persistence
    db = init_db() if persist else None
    run_id = new_id()
    if db is not None:
        with RunContext(db, "bond_health", {"ticker": ticker}, run_id=run_id):
            label_to_id = _upsert_bond_universe(
                db, debt.issuer_cik, ticker.upper(), debt.issuer_name,
                debt.bonds, cusip_map,
                debt.most_recent_10k_accession,
                debt.most_recent_10k_filed_date,
                run_id,
            )
            bundle.findings = _process_bonds(
                db, ticker, debt.bonds, label_to_id, cusip_map, curve,
                snapshot_date, run_id, verbose=verbose,
            )
            _summarize_issuer_level(bundle, debt.bonds)
            _persist_issuer_snapshot(db, bundle, run_id)
    else:
        bundle.findings = _process_bonds(
            None, ticker, debt.bonds, {}, cusip_map, curve,
            snapshot_date, run_id, verbose=verbose,
        )
        _summarize_issuer_level(bundle, debt.bonds)

    # Auth status — reflect the union of what we did:
    #   "ok"               creds work, per-CUSIP prices flowing
    #   "no_per_cusip"     creds work (macro credit fetched), but no per-CUSIP
    #                       (either no CUSIPs mapped or free-tier-only access)
    #   "no_credentials"   no creds set anywhere
    #   "auth_failed"      creds set but FINRA OAuth rejected
    if not bundle.auth_status:
        if bundle.n_priced > 0:
            bundle.auth_status = "ok"
        elif bundle.market_credit_text:
            bundle.auth_status = "no_per_cusip"
        else:
            # Macro fetch failed too — most likely no creds at all
            from ingestion.loaders.finra_trace_loader import _get_credentials
            bundle.auth_status = ("no_credentials" if _get_credentials() is None
                                   else "auth_failed")
    return bundle


def _process_bonds(
    db,
    ticker: str,
    bonds,
    label_to_id: dict,
    cusip_map: dict,
    curve,
    snapshot_date: str,
    run_id: str,
    *,
    verbose: bool = False,
) -> list:
    """Per-bond processing: prices, spread, trend stats, flags."""
    findings: list[BondHealthFinding] = []
    for b in bonds:
        finding = BondHealthFinding(
            bond_id=label_to_id.get(b.series_label, ""),
            series_label=b.series_label,
            coupon_pct=b.coupon_pct,
            par_amount_m=b.par_amount_m,
            maturity_date=b.maturity_date,
            is_callable=b.is_callable,
            cusip=cusip_map.get(b.series_label, "") or "",
        )
        if b.maturity_date:
            try:
                mat = datetime.fromisoformat(b.maturity_date).date()
                snap = datetime.fromisoformat(snapshot_date).date()
                finding.years_to_maturity = max(
                    (mat - snap).days / 365.25, 0.0
                )
            except ValueError:
                pass

        if not finding.cusip:
            finding.price_source = "no_cusip"
            findings.append(finding)
            continue

        prices = fetch_bond_prices(finding.cusip, lookback_days=180,
                                    verbose=verbose)

        # Persist price observations
        if db is not None and prices.observations:
            _upsert_bond_prices(db, finding.bond_id, finding.cusip,
                                 prices.observations, run_id)

        # If no observations, mark and continue
        if not prices.observations:
            finding.price_source = (prices.auth_status
                                     if prices.auth_status else "no_data")
            findings.append(finding)
            continue

        finding.price_source = prices.observations[-1].source

        # Compute spread per observation; persist each as a snapshot row
        if curve and finding.years_to_maturity:
            for obs in prices.observations:
                yields_dict = bond_yields(
                    obs.price, b.coupon_pct or 0, finding.years_to_maturity,
                )
                if yields_dict["ytw_pct"] is None:
                    continue
                sp = g_spread_bps(
                    yields_dict["ytw_pct"],
                    yields_dict["ytw_horizon_years"],
                    curve,
                )
                if db is not None:
                    ust = curve.interpolate(yields_dict["ytw_horizon_years"])
                    _upsert_spread_snapshot(
                        db, finding.bond_id, obs.trade_date, obs.price,
                        yields_dict["ytm_pct"], yields_dict["ytw_pct"],
                        sp, ust, yields_dict["ytw_horizon_years"], run_id,
                    )
        if db is not None:
            db.commit()

        # Latest snapshot fields
        latest = prices.observations[-1]
        finding.has_price = True
        finding.last_price = latest.price
        finding.last_price_date = latest.trade_date
        if curve and finding.years_to_maturity:
            yld = bond_yields(latest.price, b.coupon_pct or 0,
                                finding.years_to_maturity)
            finding.ytw_pct = yld["ytw_pct"]
            finding.ytw_horizon_years = yld["ytw_horizon_years"]
            finding.g_spread_bps = g_spread_bps(
                yld["ytw_pct"], yld["ytw_horizon_years"], curve,
            )

        # Trend stats from DB history
        if db is not None and finding.bond_id:
            history_6m = _get_historical_spreads(db, finding.bond_id, days=180)
            finding.spread_30d_chg_bps = _spread_change(
                history_6m, target_lookback_days=30, snapshot_date=snapshot_date)
            finding.spread_90d_chg_bps = _spread_change(
                history_6m, target_lookback_days=90, snapshot_date=snapshot_date)
            stdev, n = _spread_stdev(history_6m)
            finding.spread_stdev_6m_bps = stdev
            finding.n_snapshots_6m = n
            # Flag: 30d move > 1 stdev AND ≥ 30 days of history
            if (stdev is not None and n >= 30 and
                    finding.spread_30d_chg_bps is not None and
                    abs(finding.spread_30d_chg_bps) > stdev):
                finding.widen_1stdev_flag = finding.spread_30d_chg_bps > 0

        findings.append(finding)
    return findings


def _summarize_issuer_level(bundle: BondHealthBundle, debt_bonds: list) -> None:
    """Compute par-weighted issuer averages + divergence flag."""
    priced = [f for f in bundle.findings if f.has_price and f.g_spread_bps is not None]
    bundle.n_priced = len(priced)
    if not priced:
        return

    total_par = sum((f.par_amount_m or 0) for f in priced) or len(priced)
    def _w(field: str) -> float | None:
        vals = [(getattr(f, field), f.par_amount_m or 1) for f in priced
                if getattr(f, field) is not None]
        if not vals:
            return None
        return sum(v * w for v, w in vals) / sum(w for _, w in vals)

    bundle.avg_g_spread_bps = _w("g_spread_bps")
    bundle.avg_spread_30d_chg_bps = _w("spread_30d_chg_bps")
    bundle.avg_spread_90d_chg_bps = _w("spread_90d_chg_bps")
    bundle.n_widening_1stdev = sum(1 for f in priced if f.widen_1stdev_flag)

    # Credit-equity divergence: avg spread widening AND equity flat-or-up
    if (bundle.avg_spread_30d_chg_bps is not None
            and bundle.equity_30d_chg_pct is not None
            and bundle.avg_spread_30d_chg_bps > 10  # noise floor: 10 bps
            and bundle.equity_30d_chg_pct >= -2):  # flat-or-up tolerance
        bundle.credit_equity_divergence_flag = True


def _persist_issuer_snapshot(
    db: sqlite3.Connection,
    bundle: BondHealthBundle,
    run_id: str,
) -> None:
    existing = db.execute(
        """SELECT snapshot_id FROM issuer_credit_snapshot
           WHERE issuer_cik = ? AND snapshot_date = ?""",
        (bundle.issuer_cik, bundle.snapshot_date),
    ).fetchone()
    cols = (
        bundle.issuer_cik, bundle.ticker, bundle.snapshot_date,
        bundle.n_priced, bundle.avg_g_spread_bps,
        bundle.avg_spread_30d_chg_bps, bundle.avg_spread_90d_chg_bps,
        None,  # z_spread_stdev_6m_bps — could compute issuer-level later
        bundle.n_widening_1stdev, bundle.equity_close,
        bundle.equity_30d_chg_pct, bundle.equity_90d_chg_pct,
        1 if bundle.credit_equity_divergence_flag else 0, run_id,
    )
    if existing:
        db.execute(
            """UPDATE issuer_credit_snapshot SET
                   issuer_ticker = ?, n_bonds_priced = ?,
                   avg_z_spread_bps = ?, avg_z_spread_30d_chg_bps = ?,
                   avg_z_spread_90d_chg_bps = ?, z_spread_stdev_6m_bps = ?,
                   n_bonds_widening_1stdev = ?, equity_close = ?,
                   equity_30d_chg_pct = ?, equity_90d_chg_pct = ?,
                   credit_equity_divergence = ?, run_id = ?
               WHERE snapshot_id = ?""",
            (cols[1], *cols[3:], existing[0]),
        )
    else:
        db.execute(
            """INSERT INTO issuer_credit_snapshot
               (snapshot_id, issuer_cik, issuer_ticker, snapshot_date,
                n_bonds_priced, avg_z_spread_bps, avg_z_spread_30d_chg_bps,
                avg_z_spread_90d_chg_bps, z_spread_stdev_6m_bps,
                n_bonds_widening_1stdev, equity_close,
                equity_30d_chg_pct, equity_90d_chg_pct,
                credit_equity_divergence, run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id(), *cols),
        )
    db.commit()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _main():
    import argparse
    p = argparse.ArgumentParser(description="Issuer credit / bond health snapshot")
    p.add_argument("ticker")
    p.add_argument("--no-persist", action="store_true",
                    help="Don't write to workbench.db")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    bundle = assess_bond_health(args.ticker, persist=not args.no_persist,
                                  verbose=True)
    print()
    if args.json:
        print(json.dumps(bundle.to_dict(), indent=2, default=str))
    else:
        print(bundle.to_prompt_text() or "(no bond health data)")


if __name__ == "__main__":
    _main()
