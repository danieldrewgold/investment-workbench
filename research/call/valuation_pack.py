"""
Where the current multiple sits, computed in code, as input to a judgment.

Nobody values these names on a DCF, and a handful of peers can't say what a
stock "should" trade at. So this module doesn't produce a fair value. It lays out
the evidence for judging whether the current multiple is fair, high or low, and
whether it is more likely to compress or expand:

  current multiples      on current-FY and next-FY consensus EPS, and trailing
  own history            split-adjusted trailing P/E over five years, and the
                         EPS growth the market was paying for each year, so a
                         past multiple is always read next to the growth it bought
  context only           peer forward P/E (different growth, margins, formats)

Revisions, margin trend, guidance credibility and positioning come from the
other layers; the call stage weighs them all.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from statistics import median, quantiles

def parse_quarterly_eps(corpus_text: str) -> list[tuple[date, float]]:
    """(quarter end, diluted EPS) from the quarterly financials table, oldest first."""
    out = []
    for line in (corpus_text or "").splitlines():
        m = re.match(r"\s*Q([1-4])\s+(\d{4})\s.*\$\s*(-?[\d.]+)\s+(?:[-+][\d.]+%|—)\s*$", line)
        if m:
            q, y, eps = int(m.group(1)), int(m.group(2)), float(m.group(3))
            end_month = q * 3
            end = date(y, end_month, 30 if end_month in (6, 9) else 31)
            out.append((end, eps))
    return sorted(out)


def split_adjust(eps: list[tuple[date, float]], splits: list[tuple[date, float]]) -> list[tuple[date, float]]:
    adj = []
    for d, e in eps:
        factor = 1.0
        for sd, ratio in splits:
            if d < sd and ratio > 0:
                factor *= ratio
        adj.append((d, e / factor))
    return adj


def _ttm_series(eps: list[tuple[date, float]], report_lag_days: int):
    """Function d -> (ttm_eps, ttm_eps_year_ago) using quarters reported by date d."""
    def at(d: date):
        known = [e for (qe, e) in eps if qe + timedelta(days=report_lag_days) <= d]
        if len(known) < 8:
            return (sum(known[-4:]) if len(known) >= 4 else None), None
        return sum(known[-4:]), sum(known[-8:-4])
    return at


def pe_history(daily: list, eps: list[tuple[date, float]], years: int = 5,
               report_lag_days: int = 40) -> dict | None:
    """Monthly trailing P/E over `years`, plus each calendar year's average P/E next to
    the trailing EPS growth the market was paying for that year."""
    if len(eps) < 8 or not daily:
        return None
    # Guard: an unadjusted split shows up as a quarter-to-quarter jump of 8x or more.
    for (_, e1), (d2, e2) in zip(eps, eps[1:]):
        if e1 > 0 and e2 > 0 and max(e1, e2) / min(e1, e2) > 8:
            return {"error": f"EPS jumps {e1}->{e2} around {d2}; split history unreliable"}
    prices = {date.fromisoformat(d): p for d, p in daily}
    start = max(prices) - timedelta(days=365 * years)
    last_in_month: dict = {}
    for d in prices:
        if d >= start:
            k = (d.year, d.month)
            if k not in last_in_month or d > last_in_month[k]:
                last_in_month[k] = d
    ttm_at = _ttm_series(eps, report_lag_days)
    points = []
    for d in sorted(last_in_month.values()):
        ttm, ttm_prev = ttm_at(d)
        if not ttm or ttm <= 0:
            continue
        pe = prices[d] / ttm
        if not 3 <= pe <= 300:
            continue
        growth = (ttm / ttm_prev - 1) * 100 if ttm_prev and ttm_prev > 0 else None
        points.append((d, pe, growth))
    if len(points) < 12:
        return None
    pes = [p for _, p, _ in points]
    q = quantiles(pes, n=4)
    by_year = {}
    for d, pe, g in points:
        by_year.setdefault(d.year, {"pe": [], "g": []})
        by_year[d.year]["pe"].append(pe)
        if g is not None:
            by_year[d.year]["g"].append(g)
    yearly = [{"year": y, "avg_pe": round(sum(v["pe"]) / len(v["pe"]), 1),
               "ttm_eps_growth_pct": round(sum(v["g"]) / len(v["g"]), 1) if v["g"] else None,
               "months": len(v["pe"])} for y, v in sorted(by_year.items())]
    now_d, now_pe, now_g = points[-1]
    return {"years": years, "n_months": len(pes), "median": round(median(pes), 1),
            "p25": round(q[0], 1), "p75": round(q[2], 1), "current": round(now_pe, 1),
            "current_ttm_eps_growth_pct": round(now_g, 1) if now_g is not None else None,
            "min": round(min(pes), 1), "max": round(max(pes), 1),
            "percentile_now": round(100 * sum(1 for x in pes if x <= now_pe) / len(pes)),
            "by_year": yearly}


def _splits(ticker: str) -> list[tuple[date, float]] | None:
    try:
        import yfinance as yf
        s = yf.Ticker(ticker).splits
        return [(ix.date(), float(v)) for ix, v in s.items()]
    except Exception:
        return None


def parse_peer_pes(peer_corpus_text: str) -> list[tuple[str, float, float | None]]:
    """(ticker, forward P/E, FY EPS growth %) from the peer consensus table."""
    out = []
    for line in (peer_corpus_text or "").splitlines():
        m = re.match(r"^([A-Z.]{1,6})\s+[+-][\d.]+%\s+([+-][\d.]+)%\s+([\d.]+)×", line.strip())
        if m:
            out.append((m.group(1), float(m.group(3)), float(m.group(2))))
    return out


def build(ticker: str, price: float, cons_fy_eps: float | None, cons_next_eps: float | None,
          quarterly_corpus: str, daily_prices: list, peer_corpus: str = "") -> dict:
    pack: dict = {"price": price, "cons_fy_eps": cons_fy_eps, "cons_next_eps": cons_next_eps}
    if cons_next_eps and cons_next_eps > 0:
        pack["fwd_pe_next"] = round(price / cons_next_eps, 1)
    if cons_fy_eps and cons_fy_eps > 0:
        pack["pe_current_fy"] = round(price / cons_fy_eps, 1)
    if cons_fy_eps and cons_next_eps and cons_fy_eps > 0:
        pack["cons_next_fy_growth_pct"] = round((cons_next_eps / cons_fy_eps - 1) * 100, 1)
    eps = parse_quarterly_eps(quarterly_corpus)
    splits = _splits(ticker)
    if splits is None:
        pack["own_pe_history"] = {"error": "split history unavailable; own P/E history skipped"}
    else:
        pack["own_pe_history"] = pe_history(daily_prices, split_adjust(eps, splits)) or             {"error": "not enough history"}
        pack["splits"] = [(d.isoformat(), r) for d, r in splits]
    pack["peers"] = [{"ticker": t, "fwd_pe": pe, "fy_eps_growth_pct": g} for t, pe, g in parse_peer_pes(peer_corpus)]
    return pack


def render_block(pack: dict) -> str:
    if not pack:
        return ""
    L = [f"=== WHERE THE MULTIPLE SITS (computed; price ${pack['price']:,.2f}) ==="]
    if pack.get("fwd_pe_next"):
        L.append(f"P/E on next-FY consensus EPS ${pack['cons_next_eps']:.2f}: {pack['fwd_pe_next']}x"
                 + (f"; on current-FY ${pack['cons_fy_eps']:.2f}: {pack['pe_current_fy']}x" if pack.get("pe_current_fy") else "")
                 + (f". Consensus next-FY EPS growth {pack['cons_next_fy_growth_pct']:+.1f}%." if pack.get("cons_next_fy_growth_pct") is not None else ""))
    h = pack.get("own_pe_history") or {}
    if h.get("median"):
        L.append(f"Own trailing P/E, last {h['years']} years (split-adjusted): now {h['current']}x "
                 f"({h['percentile_now']}th percentile), median {h['median']}x, middle half {h['p25']}x to "
                 f"{h['p75']}x, range {h['min']}x to {h['max']}x. Trailing EPS growth now "
                 f"{h['current_ttm_eps_growth_pct']:+.1f}%." if h.get("current_ttm_eps_growth_pct") is not None else
                 f"Own trailing P/E: now {h['current']}x, median {h['median']}x.")
        L.append("What the market paid for growth, by year (average trailing P/E vs trailing EPS growth):")
        for y in h.get("by_year") or []:
            g = y["ttm_eps_growth_pct"]
            L.append(f"  {y['year']}: {y['avg_pe']}x for {f'{g:+.1f}%' if g is not None else 'n/a'} EPS growth "
                     f"({y['months']} months)")
    elif h.get("error"):
        L.append(f"Own P/E history unavailable: {h['error']}")
    if pack.get("peers"):
        L.append("Context only, peer forward P/E with FY EPS growth (different growth, margins and formats): "
                 + ", ".join(f"{p['ticker']} {p['fwd_pe']}x ({p['fy_eps_growth_pct']:+.1f}%)" for p in pack["peers"]))
    return "\n".join(L)
