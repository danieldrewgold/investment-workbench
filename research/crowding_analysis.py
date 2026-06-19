"""
13F Crowding Analysis

Sits DOWNSTREAM of the core research engine, alongside market_overlay.
Produces a CROWDING_ASSESSMENT workpaper with:
  - how many tracked hedge funds hold the name
  - AUM-weighted crowding score (0-100)
  - entry/exit trends (who's accumulating, who's exiting)
  - historical percentile (is current crowding extreme vs history?)
  - peer-relative percentile (crowded vs peers?)
  - variant signal interpretation for the research brain

Uses SEC EDGAR 13F-HR filings (quarterly, 45 days stale).
Data layer: fund_universe, filing_13f, holding_13f, crowding_snapshot.

This module NEVER:
  - drives schema selection
  - replaces estimate work
  - overrides the core thesis
  - generates trade signals from ownership alone
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.provenance.database import new_id, upsert, now_iso


@dataclass
class CrowdingHolder:
    """A single fund's position in the target stock."""
    fund_name: str = ""
    fund_type: str = ""
    value_m: float = 0.0           # position value in millions (current quarter)
    pct_of_fund: float = 0.0       # position as % of fund's total portfolio
    shares: int = 0
    quarters_held: int = 0
    entry_exit: str = "HOLD"       # HOLD, NEW_ENTRY, INCREASED, DECREASED, EXITED
    # QoQ delta detail — populated when we have a prior-quarter comparison
    prior_value_m: float | None = None      # position $M last quarter (None if NEW_ENTRY)
    delta_value_m: float | None = None      # change in $M (signed)
    delta_pct: float | None = None          # % change (None if NEW_ENTRY or zero base)


@dataclass
class CrowdingAssessment:
    """The crowding module's output: ownership concentration analysis."""
    ticker: str = ""
    cusip: str = ""
    report_date: str = ""           # the quarter used for trend analysis
                                     # (= fully_reported_quarter when possible)

    # Reporting-cycle context — distinguish "fully-reported" reference
    # quarter from any in-flight quarter where 13F filings are still
    # arriving. This avoids the false "everyone exited" signal when the
    # latest quarter only has a handful of early filers.
    latest_filed_quarter: str = ""   # MAX(report_date) across all filings
    fully_reported_quarter: str = "" # most recent date with >=50% universe filed
    funds_filed_latest: int = 0      # how many tracked funds filed for latest_filed_quarter
    funds_filed_fully_reported: int = 0  # ditto for fully_reported_quarter
    prior_quarter: str = ""          # the QoQ comparison "from" date

    # Core metrics
    funds_holding: int = 0
    funds_tracked: int = 0
    ownership_pct: float = 0.0     # funds_holding / funds_tracked
    weighted_score: float = 0.0    # 0-100, AUM-weighted
    crowding_level: str = ""       # LOW, NORMAL, HIGH, EXTREME

    # Trend
    net_entries: int = 0
    net_exits: int = 0
    entry_exit_trend: str = ""     # ACCUMULATION, DISTRIBUTION, STABLE

    # Percentiles
    historical_percentile: float = None  # 0-100 vs own history
    peer_percentile: float = None        # 0-100 vs peer group
    quarters_of_history: int = 0

    # Position detail
    avg_position_pct: float = 0.0  # avg position as % of fund portfolio
    top_holders: list = field(default_factory=list)  # list of CrowdingHolder

    # Per-fund flow — top 5 funds that added vs top 5 that cut, comparing
    # the fully_reported_quarter to its prior quarter. Surfaces the actual
    # disagreement (e.g. "Citadel cut 50%, FMR added 47%") that a simple
    # holders-count summary hides.
    top_buyers: list = field(default_factory=list)   # list of CrowdingHolder
    top_sellers: list = field(default_factory=list)  # list of CrowdingHolder

    # Interpretation
    variant_signal: str = ""       # human-readable for research brain
    data_quality: str = "none"     # good, partial, minimal, none
    caveats: list = field(default_factory=list)

    run_id: str = ""


def compute_crowding(
    conn, company_id: str, cusip: str, run_id: str, quarters_back: int = 8,
) -> CrowdingAssessment:
    """
    Compute crowding score for a company from 13F holdings data.
    Reads from holding_13f, fund_universe, filing_13f tables.
    Writes to crowding_snapshot table.
    """
    ca = CrowdingAssessment(run_id=run_id, cusip=cusip)

    # Get company ticker
    row = conn.execute(
        "SELECT ticker FROM company WHERE company_id = ?", (company_id,)
    ).fetchone()
    ca.ticker = row[0] if row else ""

    # Determine latest quarter: use the most recent filing quarter across all funds,
    # not just quarters where this CUSIP has holdings. This correctly detects
    # the case where all funds have exited (0 holdings in the latest quarter).
    latest_filing = conn.execute(
        "SELECT MAX(report_date) FROM filing_13f",
    ).fetchone()

    # Also check if this CUSIP has ANY historical holdings
    latest_holding = conn.execute(
        "SELECT MAX(report_date) FROM holding_13f WHERE cusip = ?",
        (cusip,),
    ).fetchone()

    if not latest_filing or not latest_filing[0]:
        ca.data_quality = "none"
        ca.variant_signal = "No 13F filings data available. Run ingestion first: ingest --crowding"
        ca.caveats.append("No 13F filings found.")
        return ca

    if not latest_holding or not latest_holding[0]:
        ca.data_quality = "none"
        ca.variant_signal = "No 13F holdings data available for this CUSIP."
        ca.caveats.append("No 13F data found for this CUSIP. Check CUSIP mapping.")
        return ca

    # Distinguish the latest quarter (which may be in-flight) from the
    # most recent fully-reported quarter. Trend analysis MUST use
    # fully-reported, otherwise the early-filer subset distorts QoQ
    # comparisons (e.g. Q1 13Fs from inverse ETFs landing first creates
    # a false 'all funds exited' signal).
    ca.latest_filed_quarter = latest_filing[0]
    universe_size = conn.execute(
        "SELECT COUNT(*) FROM fund_universe WHERE is_active = 1"
    ).fetchone()[0]
    ca.funds_tracked = universe_size if universe_size > 0 else 0

    if ca.funds_tracked == 0:
        ca.data_quality = "none"
        ca.variant_signal = "No fund universe configured."
        return ca

    # Walk quarters newest-first; pick the first one where >=50% of the
    # universe filed. That's our trend-reference quarter.
    quarter_fill = conn.execute("""
        SELECT report_date, COUNT(DISTINCT fund_id) AS n_filed
        FROM filing_13f
        WHERE fund_id IN (SELECT fund_id FROM fund_universe WHERE is_active = 1)
        GROUP BY report_date
        ORDER BY report_date DESC
    """).fetchall()
    fill_by_date = {rd: n for rd, n in quarter_fill}
    ca.funds_filed_latest = fill_by_date.get(ca.latest_filed_quarter, 0)

    fully_reported = None
    for rd, n_filed in quarter_fill:
        if n_filed >= max(1, universe_size * 0.5):
            fully_reported = rd
            ca.funds_filed_fully_reported = n_filed
            break
    if fully_reported is None:
        # No quarter is at-50% yet — fall back to latest available with at
        # least 1 filer. Trend results will be flagged as low-confidence.
        fully_reported = ca.latest_filed_quarter
        ca.funds_filed_fully_reported = ca.funds_filed_latest
    ca.fully_reported_quarter = fully_reported
    ca.report_date = fully_reported   # the rest of compute_crowding uses this

    # In-flight latest-quarter caveat
    if (ca.latest_filed_quarter != ca.fully_reported_quarter
            or ca.funds_filed_latest < universe_size):
        ca.caveats.append(
            f"Latest quarter ({ca.latest_filed_quarter}) is in-flight: "
            f"{ca.funds_filed_latest}/{universe_size} tracked funds reporting "
            f"so far (13F deadline is 45 days post-quarter). Trend metrics "
            f"below use fully-reported quarter {ca.fully_reported_quarter} "
            f"({ca.funds_filed_fully_reported}/{universe_size} funds)."
        )

    # Funds holding this CUSIP in the latest quarter
    # Aggregate by fund_id to handle funds with multiple position types (shares + puts/calls)
    holders = conn.execute("""
        SELECT h.fund_id, f.fund_name, f.fund_type,
               SUM(h.value_thousands) as total_value_thousands,
               SUM(h.shares_or_amount) as total_shares,
               fi.total_value_m
        FROM holding_13f h
        JOIN fund_universe f ON h.fund_id = f.fund_id
        JOIN filing_13f fi ON h.filing_id = fi.filing_id
        WHERE h.cusip = ? AND h.report_date = ?
        GROUP BY h.fund_id, f.fund_name, f.fund_type, fi.total_value_m
        ORDER BY total_value_thousands DESC
    """, (cusip, ca.report_date)).fetchall()

    ca.funds_holding = len(holders)
    ca.ownership_pct = round(ca.funds_holding / ca.funds_tracked * 100, 1) if ca.funds_tracked > 0 else 0

    # -- AUM-weighted crowding score -----------------------------
    # Weight = (position_value / fund_total_value) * (fund_total_value / sum_all_fund_values)
    # This captures both concentration within funds and cross-fund of large funds

    total_tracked_aum = conn.execute("""
        SELECT SUM(total_value_m) FROM filing_13f
        WHERE report_date = ? AND fund_id IN (SELECT fund_id FROM fund_universe WHERE is_active = 1)
    """, (ca.report_date,)).fetchone()[0] or 1.0

    weighted_sum = 0.0
    position_pcts = []

    for h in holders:
        fund_id, fund_name, fund_type, value_k, shares, fund_total_m = h
        position_value_m = value_k / 1000.0  # thousands -> millions
        fund_total_m = fund_total_m or 1.0

        position_pct = (position_value_m / fund_total_m * 100) if fund_total_m > 0 else 0
        aum_weight = fund_total_m / total_tracked_aum if total_tracked_aum > 0 else 0

        weighted_sum += position_pct * aum_weight
        position_pcts.append(position_pct)

    # Normalize to 0-100 scale. Practical maximum is ~20-30 for heavily crowded names.
    # Use 30 as the normalization ceiling so scores are meaningful in the 0-100 range.
    ca.weighted_score = round(min(weighted_sum / 30 * 100, 100), 1)
    ca.avg_position_pct = round(sum(position_pcts) / len(position_pcts), 2) if position_pcts else 0

    # -- Entry/exit tracking -------------------------------------
    # Compare current holders to prior quarter (where prior MUST also be
    # at least 50%-reported to avoid the same in-flight bias).
    prior_date = conn.execute("""
        SELECT report_date FROM filing_13f
        WHERE fund_id IN (SELECT fund_id FROM fund_universe WHERE is_active = 1)
          AND report_date < ?
        GROUP BY report_date
        HAVING COUNT(DISTINCT fund_id) >= ?
        ORDER BY report_date DESC LIMIT 1
    """, (ca.report_date, max(1, universe_size // 2))).fetchone()
    if prior_date:
        ca.prior_quarter = prior_date[0]

    prior_holders_set = set()
    prior_values = {}
    if prior_date:
        prior_rows = conn.execute("""
            SELECT fund_id, value_thousands FROM holding_13f
            WHERE cusip = ? AND report_date = ?
        """, (cusip, prior_date[0])).fetchall()
        prior_holders_set = {r[0] for r in prior_rows}
        prior_values = {r[0]: r[1] for r in prior_rows}

    current_holders_set = {h[0] for h in holders}

    if prior_date:
        new_entries = current_holders_set - prior_holders_set
        exits = prior_holders_set - current_holders_set
        ca.net_entries = len(new_entries)
        ca.net_exits = len(exits)

        if ca.net_entries >= 5 and ca.net_exits <= 1:
            ca.entry_exit_trend = "ACCUMULATION"
        elif ca.net_exits >= 5 and ca.net_entries <= 1:
            ca.entry_exit_trend = "DISTRIBUTION"
        elif ca.net_entries >= 3 and ca.net_entries > ca.net_exits * 2:
            ca.entry_exit_trend = "ACCUMULATION"
        elif ca.net_exits >= 3 and ca.net_exits > ca.net_entries * 2:
            ca.entry_exit_trend = "DISTRIBUTION"
        else:
            ca.entry_exit_trend = "STABLE"
    else:
        # No prior quarter to compare -- first quarter of data
        new_entries = set()
        exits = set()
        ca.net_entries = 0
        ca.net_exits = 0
        ca.entry_exit_trend = "STABLE"

    # -- Build top holders list ----------------------------------
    for h in holders[:10]:
        fund_id, fund_name, fund_type, value_k, shares, fund_total_m = h
        position_value_m = value_k / 1000.0
        fund_total_m = fund_total_m or 1.0
        pct_of_fund = round(position_value_m / fund_total_m * 100, 2) if fund_total_m > 0 else 0

        # Determine entry/exit status
        if fund_id in new_entries:
            status = "NEW_ENTRY"
        elif fund_id in prior_values:
            prior_val = prior_values[fund_id]
            if value_k > prior_val * 1.1:
                status = "INCREASED"
            elif value_k < prior_val * 0.9:
                status = "DECREASED"
            else:
                status = "HOLD"
        else:
            status = "HOLD"

        # Count quarters held
        qh = conn.execute("""
            SELECT COUNT(DISTINCT report_date) FROM holding_13f
            WHERE fund_id = ? AND cusip = ?
        """, (fund_id, cusip)).fetchone()[0]

        # Per-fund QoQ delta (signed change in $value)
        prior_val_k = prior_values.get(fund_id)
        prior_val_m = (prior_val_k / 1000.0) if prior_val_k is not None else None
        delta_v_m = (round(position_value_m - prior_val_m, 1)
                       if prior_val_m is not None else None)
        delta_pct = None
        if prior_val_m is not None and prior_val_m > 0:
            delta_pct = round((position_value_m - prior_val_m) / prior_val_m * 100, 1)

        ca.top_holders.append(CrowdingHolder(
            fund_name=fund_name,
            fund_type=fund_type,
            value_m=round(position_value_m, 1),
            pct_of_fund=pct_of_fund,
            shares=int(shares) if shares else 0,
            quarters_held=qh,
            entry_exit=status,
            prior_value_m=round(prior_val_m, 1) if prior_val_m is not None else None,
            delta_value_m=delta_v_m,
            delta_pct=delta_pct,
        ))

    # -- Top buyers / top sellers (per-fund QoQ flow) -------------
    # Surface the actual disagreement among funds — Citadel cut 50% while
    # FMR added 47% is a much richer signal than "13 net exits". Walks
    # ALL funds with QoQ activity (current + prior union), ranks by
    # signed delta, splits into adders and trimmers/exiters.
    if prior_date and (current_holders_set or prior_holders_set):
        # Map current quarter values for fund_ids the user already
        # iterated over above, plus pull data for funds that EXITED
        # (held in prior, not in current).
        # holders tuple: (fund_id, fund_name, fund_type, value_k, shares, fund_total_m)
        fund_id_to_current: dict = {
            h[0]: {"name": h[1], "type": h[2], "value_k": h[3]} for h in holders
        }
        fund_id_to_prior: dict = {}
        if prior_holders_set:
            prior_rows_full = conn.execute("""
                SELECT h.fund_id, f.fund_name, f.fund_type, h.value_thousands
                FROM holding_13f h JOIN fund_universe f ON h.fund_id = f.fund_id
                WHERE h.cusip = ? AND h.report_date = ?
            """, (cusip, prior_date[0])).fetchall()
            fund_id_to_prior = {
                r[0]: {"name": r[1], "type": r[2], "value_k": r[3]}
                for r in prior_rows_full
            }

        flow_rows: list[tuple[float, dict]] = []  # (delta_m, payload)
        all_fund_ids = current_holders_set | prior_holders_set
        for fid in all_fund_ids:
            cur = fund_id_to_current.get(fid) or {}
            prior = fund_id_to_prior.get(fid) or {}
            cur_val_k = cur.get("value_k") or 0
            prior_val_k = prior.get("value_k") or 0
            fund_name_x = cur.get("name") or prior.get("name") or "(unknown)"
            fund_type_x = cur.get("type") or prior.get("type") or ""
            cur_m = cur_val_k / 1000.0
            prior_m = prior_val_k / 1000.0
            delta_m = round(cur_m - prior_m, 1)
            delta_pct_x = None
            if prior_m > 0:
                delta_pct_x = round((cur_m - prior_m) / prior_m * 100, 1)
            elif cur_m > 0:
                delta_pct_x = None  # NEW_ENTRY — no base
            # Status
            if fid not in prior_holders_set and fid in current_holders_set:
                status_x = "NEW_ENTRY"
            elif fid in prior_holders_set and fid not in current_holders_set:
                status_x = "EXITED"
            elif cur_val_k > prior_val_k * 1.1:
                status_x = "INCREASED"
            elif cur_val_k < prior_val_k * 0.9:
                status_x = "DECREASED"
            else:
                status_x = "HOLD"
            flow_rows.append((delta_m, {
                "fund_name": fund_name_x, "fund_type": fund_type_x,
                "value_m": round(cur_m, 1),
                "prior_value_m": round(prior_m, 1) if prior_m > 0 else None,
                "delta_value_m": delta_m,
                "delta_pct": delta_pct_x,
                "entry_exit": status_x,
            }))

        # Filter out funds with negligible movement (|delta| < 0.5M) so
        # tiny rebalances don't crowd out real signal.
        meaningful = [r for r in flow_rows if abs(r[0]) >= 0.5]
        meaningful.sort(key=lambda r: r[0], reverse=True)
        for delta_m, payload in meaningful[:5]:
            if delta_m > 0:
                ca.top_buyers.append(CrowdingHolder(**payload))
        meaningful.sort(key=lambda r: r[0])
        for delta_m, payload in meaningful[:5]:
            if delta_m < 0:
                ca.top_sellers.append(CrowdingHolder(**payload))

    # -- Historical percentile -----------------------------------
    # Get all historical scores for this CUSIP
    all_dates = conn.execute("""
        SELECT DISTINCT report_date FROM holding_13f WHERE cusip = ?
        ORDER BY report_date
    """, (cusip,)).fetchall()
    ca.quarters_of_history = len(all_dates)

    historical_scores = []
    for (rd,) in all_dates:
        if rd == ca.report_date:
            historical_scores.append(ca.weighted_score)
            continue
        snap = conn.execute(
            "SELECT weighted_score FROM crowding_snapshot WHERE company_id = ? AND report_date = ?",
            (company_id, rd),
        ).fetchone()
        if snap:
            historical_scores.append(snap[0])

    if len(historical_scores) >= 2:
        below = sum(1 for s in historical_scores if s < ca.weighted_score)
        ca.historical_percentile = round(below / len(historical_scores) * 100, 0)

    # -- Peer-relative percentile --------------------------------
    peer_scores = conn.execute("""
        SELECT cs.weighted_score
        FROM crowding_snapshot cs
        JOIN peer_relationship pr ON cs.company_id = pr.peer_company_id
        WHERE pr.company_id = ? AND cs.report_date = ?
    """, (company_id, ca.report_date)).fetchall()

    if peer_scores:
        peer_vals = [r[0] for r in peer_scores]
        below = sum(1 for s in peer_vals if s < ca.weighted_score)
        ca.peer_percentile = round(below / len(peer_vals) * 100, 0)

    # -- Crowding level classification ---------------------------
    hist_pct = ca.historical_percentile if ca.historical_percentile is not None else 50
    peer_pct = ca.peer_percentile if ca.peer_percentile is not None else 50

    if hist_pct > 90 and peer_pct > 80:
        ca.crowding_level = "EXTREME"
    elif hist_pct > 75 or peer_pct > 80:
        ca.crowding_level = "HIGH"
    elif hist_pct < 25 and peer_pct < 20:
        ca.crowding_level = "LOW"
    elif hist_pct < 10:
        ca.crowding_level = "LOW"
    else:
        ca.crowding_level = "NORMAL"

    # -- Data quality --------------------------------------------
    if ca.funds_tracked >= 30 and ca.quarters_of_history >= 4:
        ca.data_quality = "good"
    elif ca.funds_tracked >= 10 and ca.quarters_of_history >= 2:
        ca.data_quality = "partial"
    elif ca.funds_tracked > 0:
        ca.data_quality = "minimal"
    else:
        ca.data_quality = "none"

    # -- Variant signal interpretation ---------------------------
    ca.variant_signal = _build_variant_signal(ca)

    # -- Caveats -------------------------------------------------
    ca.caveats.append(
        f"13F data is 45 days stale. Report date: {ca.report_date}. "
        "Positions may have changed since filing."
    )
    if ca.funds_tracked < 30:
        ca.caveats.append(
            f"Only {ca.funds_tracked} funds tracked. Score reliability improves with broader coverage."
        )
    if ca.quarters_of_history < 4:
        ca.caveats.append(
            f"Only {ca.quarters_of_history} quarters of history. Historical percentile may not be meaningful."
        )

    # -- Store snapshot ------------------------------------------
    upsert(conn, "crowding_snapshot", {
        "snapshot_id": new_id(),
        "company_id": company_id,
        "cusip": cusip,
        "report_date": ca.report_date,
        "funds_holding": ca.funds_holding,
        "funds_tracked": ca.funds_tracked,
        "ownership_pct": ca.ownership_pct,
        "weighted_score": ca.weighted_score,
        "net_entries": ca.net_entries,
        "net_exits": ca.net_exits,
        "avg_position_pct": ca.avg_position_pct,
        "historical_percentile": ca.historical_percentile,
        "run_id": run_id,
        "created_at": now_iso(),
    }, conflict_columns=["company_id", "report_date"],
    update_columns=[
        "funds_holding", "funds_tracked", "ownership_pct", "weighted_score",
        "net_entries", "net_exits", "avg_position_pct", "historical_percentile",
        "run_id",
    ])
    conn.commit()

    return ca


def _build_variant_signal(ca: CrowdingAssessment) -> str:
    """Build human-readable variant signal interpretation."""
    parts = []

    # Crowding level
    if ca.crowding_level == "EXTREME":
        parts.append(
            f"{ca.ticker} is an extremely crowded institutional long. "
            f"{ca.funds_holding}/{ca.funds_tracked} tracked hedge funds hold the name "
            f"(score {ca.weighted_score:.0f}/100). "
            "If one major holder exits, others may follow -- the unwind risk is elevated."
        )
    elif ca.crowding_level == "HIGH":
        parts.append(
            f"{ca.ticker} is a crowded hedge fund holding. "
            f"{ca.funds_holding}/{ca.funds_tracked} tracked funds own it "
            f"(score {ca.weighted_score:.0f}/100). "
            "Crowded longs are vulnerable to coordinated de-risking events."
        )
    elif ca.crowding_level == "LOW":
        parts.append(
            f"{ca.ticker} has LOW institutional crowding. "
            f"Only {ca.funds_holding}/{ca.funds_tracked} tracked hedge funds hold it "
            f"(score {ca.weighted_score:.0f}/100). "
            "This is a legitimate variant perception -- the name is under-owned relative to "
            "the hedge fund universe."
        )
    else:
        parts.append(
            f"{ca.ticker} has NORMAL institutional crowding. "
            f"{ca.funds_holding}/{ca.funds_tracked} tracked funds hold it "
            f"(score {ca.weighted_score:.0f}/100)."
        )

    # Trend
    if ca.entry_exit_trend == "ACCUMULATION":
        parts.append(
            f"Trend: ACCUMULATION -- {ca.net_entries} funds entered vs {ca.net_exits} exits. "
            "Smart money is building positions."
        )
    elif ca.entry_exit_trend == "DISTRIBUTION":
        parts.append(
            f"Trend: DISTRIBUTION -- {ca.net_exits} funds exited vs {ca.net_entries} entries. "
            "Watch for further exits. When institutional holders head for the same door, "
            "the unwind can be violent."
        )

    # Historical context
    if ca.historical_percentile is not None:
        if ca.historical_percentile >= 90:
            parts.append(
                f"At the {ca.historical_percentile:.0f}th percentile of its own history -- "
                "historically high crowding."
            )
        elif ca.historical_percentile <= 10:
            parts.append(
                f"At the {ca.historical_percentile:.0f}th percentile of its own history -- "
                "historically low ownership."
            )

    # Peer context
    if ca.peer_percentile is not None:
        if ca.peer_percentile >= 80:
            parts.append(f"More crowded than {ca.peer_percentile:.0f}% of peers.")
        elif ca.peer_percentile <= 20:
            parts.append(f"Less crowded than {100 - ca.peer_percentile:.0f}% of peers.")

    return " ".join(parts)


async def get_crowding_for_ticker(
    conn, ticker: str, run_id: str,
    cusip: str = None, force_refresh: bool = False,
) -> CrowdingAssessment:
    """
    Main entry point for the research brain.
    Gets crowding assessment for a ticker, triggering data refresh if needed.
    """
    # Resolve company_id
    row = conn.execute(
        "SELECT company_id FROM company WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    if not row:
        ca = CrowdingAssessment(ticker=ticker, run_id=run_id)
        ca.data_quality = "none"
        ca.variant_signal = f"Company {ticker} not found in database."
        return ca

    company_id = row[0]

    # Resolve CUSIP if not provided
    if not cusip:
        cusip_row = conn.execute(
            "SELECT cusip FROM cusip_mapping WHERE ticker = ? OR company_id = ? LIMIT 1",
            (ticker.upper(), company_id),
        ).fetchone()
        if cusip_row:
            cusip = cusip_row[0]
        else:
            # Try to find from existing holdings by matching issuer name
            name_row = conn.execute(
                "SELECT name FROM company WHERE company_id = ?", (company_id,)
            ).fetchone()
            if name_row:
                # Fuzzy match: search holdings for company name
                holding_row = conn.execute(
                    "SELECT DISTINCT cusip FROM holding_13f WHERE UPPER(issuer_name) LIKE ? LIMIT 1",
                    (f"%{name_row[0].upper()[:10]}%",),
                ).fetchone()
                if holding_row:
                    cusip = holding_row[0]

    if not cusip:
        ca = CrowdingAssessment(ticker=ticker, run_id=run_id)
        ca.data_quality = "none"
        ca.variant_signal = (
            f"No CUSIP mapping found for {ticker}. "
            "Provide CUSIP with --cusip flag or run 13F ingestion first."
        )
        return ca

    # Check for recent snapshot (within 50 days of latest quarter end)
    if not force_refresh:
        recent = conn.execute("""
            SELECT snapshot_id, report_date FROM crowding_snapshot
            WHERE company_id = ? ORDER BY report_date DESC LIMIT 1
        """, (company_id,)).fetchone()
        if recent:
            snap_date = datetime.strptime(recent[1], "%Y-%m-%d")
            if (datetime.now() - snap_date).days < 100:  # within ~1 quarter
                return compute_crowding(conn, company_id, cusip, run_id)

    # If force_refresh, trigger ingestion
    if force_refresh:
        from ingestion.loaders.edgar_13f_loader import Edgar13FLoader
        loader = Edgar13FLoader(conn, run_id)
        try:
            result = await loader.ingest_all_funds(quarters_back=4)
        finally:
            await loader.close()

    return compute_crowding(conn, company_id, cusip, run_id)


# ===============================================================
# Workpaper Production
# ===============================================================

def produce_crowding_workpaper(conn, company_id: str, ca: CrowdingAssessment,
                                run_id: str = None) -> str:
    """Produce a CROWDING_ASSESSMENT workpaper."""
    from research.escalation import WorkpaperBuilder

    wb = WorkpaperBuilder(conn, company_id)

    content = {
        "ticker": ca.ticker,
        "cusip": ca.cusip,
        "report_date": ca.report_date,
        "funds_holding": ca.funds_holding,
        "funds_tracked": ca.funds_tracked,
        "ownership_pct": ca.ownership_pct,
        "weighted_score": ca.weighted_score,
        "crowding_level": ca.crowding_level,
        "net_entries": ca.net_entries,
        "net_exits": ca.net_exits,
        "entry_exit_trend": ca.entry_exit_trend,
        "historical_percentile": ca.historical_percentile,
        "peer_percentile": ca.peer_percentile,
        "avg_position_pct": ca.avg_position_pct,
        "top_holders": [
            {
                "fund_name": h.fund_name,
                "fund_type": h.fund_type,
                "value_m": h.value_m,
                "pct_of_fund": h.pct_of_fund,
                "quarters_held": h.quarters_held,
                "entry_exit": h.entry_exit,
            }
            for h in ca.top_holders
        ],
        "variant_signal": ca.variant_signal,
        "data_quality": ca.data_quality,
        "caveats": ca.caveats,
    }

    wid = wb.create(
        workpaper_type="CROWDING_ASSESSMENT",
        title=f"{ca.ticker} 13F Crowding Analysis",
        content=content,
        question="How crowded is this name among institutional holders, and what does the positioning trend signal?",
        methodology=(
            "13F crowding analysis from SEC EDGAR filings. "
            f"Tracked universe: {ca.funds_tracked} hedge funds/active managers. "
            f"AUM-weighted score (0-100), entry/exit tracking, historical + peer percentiles. "
            f"Data quality: {ca.data_quality}. "
            "13F data is 45 days stale by regulation."
        ),
        run_id=run_id,
    )

    return wid


# ===============================================================
# CLI Formatting
# ===============================================================

def format_crowding(ca: CrowdingAssessment) -> str:
    """Format the crowding assessment for CLI display."""
    lines = []
    lines.append(f"\n-- 13F Crowding Analysis ({ca.data_quality} data) --")

    if ca.data_quality == "none":
        lines.append(f"  {ca.variant_signal}")
        return "\n".join(lines)

    # Header metrics
    lines.append(f"  Ticker: {ca.ticker} | CUSIP: {ca.cusip} | Quarter: {ca.report_date}")
    lines.append(f"  Crowding level: {ca.crowding_level} (score {ca.weighted_score:.0f}/100)")
    lines.append(f"  Funds holding: {ca.funds_holding}/{ca.funds_tracked} tracked ({ca.ownership_pct:.1f}%)")
    lines.append(f"  Avg position: {ca.avg_position_pct:.2f}% of fund portfolio")

    # Trend
    lines.append(f"\n  Entry/Exit: +{ca.net_entries} entries, -{ca.net_exits} exits -> {ca.entry_exit_trend}")

    # Percentiles
    if ca.historical_percentile is not None:
        lines.append(f"  Historical percentile: {ca.historical_percentile:.0f}th ({ca.quarters_of_history}Q history)")
    if ca.peer_percentile is not None:
        lines.append(f"  Peer percentile: {ca.peer_percentile:.0f}th")

    # Top holders
    if ca.top_holders:
        lines.append(f"\n  Top holders:")
        for h in ca.top_holders[:10]:
            status_icon = {
                "NEW_ENTRY": "+NEW", "INCREASED": "+INC",
                "DECREASED": "-DEC", "EXITED": "-EXIT", "HOLD": "HOLD",
            }.get(h.entry_exit, "    ")
            lines.append(
                f"    [{status_icon:>5}] {h.fund_name:<35s} ${h.value_m:>8.1f}M "
                f"({h.pct_of_fund:.1f}% of fund, {h.quarters_held}Q)"
            )

    # Variant signal
    lines.append(f"\n  Signal: {ca.variant_signal}")

    # Caveats
    if ca.caveats:
        for c in ca.caveats:
            lines.append(f"  * {c}")

    return "\n".join(lines)


def format_crowding_history(conn, company_id: str, cusip: str) -> str:
    """Format historical crowding trend for CLI display."""
    rows = conn.execute("""
        SELECT report_date, funds_holding, funds_tracked, weighted_score,
               net_entries, net_exits, ownership_pct
        FROM crowding_snapshot
        WHERE company_id = ?
        ORDER BY report_date
    """, (company_id,)).fetchall()

    if not rows:
        return "  No historical crowding data."

    lines = ["\n  Historical Crowding Trend:"]
    lines.append(f"  {'Quarter':<12s} {'Funds':>6s} {'Score':>6s} {'Own%':>6s} {'Enter':>6s} {'Exit':>6s}")
    lines.append(f"  {'-' * 50}")

    for r in rows:
        lines.append(
            f"  {r[0]:<12s} {r[1]:>3d}/{r[2]:<3d} {r[3]:>5.0f} {r[6]:>5.1f}% "
            f"{'+' + str(r[4]):>5s} {'-' + str(r[5]):>5s}"
        )

    return "\n".join(lines)
