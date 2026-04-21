"""
Valuation Layer

Translates EPS estimates into price targets using multiple-based valuation.
Not a full DCF -- this is a screening tool, not a sell-side model.

Takes our forward EPS estimate + a valuation multiple -> implied price.
Compares to current price -> upside/downside %.
Sensitivity: +/- 3 PE turns for a range.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ValuationAssessment:
    """Result of multiple-based valuation."""
    ticker: str = ""
    # Core valuation
    implied_price: float = 0        # our_eps * applied_multiple
    current_price: float = 0
    upside_pct: float = 0           # (implied - current) / current * 100
    # Multiple used
    applied_multiple: float = 0     # PE ratio
    multiple_source: str = ""       # "forward_pe" / "trailing_pe" / "sector"
    # Sensitivity range
    multiple_low: float = 0
    multiple_high: float = 0
    price_at_low: float = 0
    price_at_high: float = 0
    upside_at_low: float = 0
    upside_at_high: float = 0
    # Context
    trailing_pe: float = 0
    forward_pe: float = 0
    consensus_implied_price: float = 0  # consensus_eps * forward_pe
    # Narrative
    narrative: str = ""

    def to_dict(self) -> dict:
        return {
            "implied_price": round(self.implied_price, 2),
            "current_price": round(self.current_price, 2),
            "upside_pct": round(self.upside_pct, 1),
            "applied_multiple": round(self.applied_multiple, 1),
            "multiple_source": self.multiple_source,
            "sensitivity": {
                "low_pe": round(self.multiple_low, 1),
                "high_pe": round(self.multiple_high, 1),
                "price_at_low": round(self.price_at_low, 2),
                "price_at_high": round(self.price_at_high, 2),
                "upside_at_low": round(self.upside_at_low, 1),
                "upside_at_high": round(self.upside_at_high, 1),
            },
            "context": {
                "trailing_pe": round(self.trailing_pe, 1),
                "forward_pe": round(self.forward_pe, 1),
                "consensus_implied": round(self.consensus_implied_price, 2),
            },
            "narrative": self.narrative,
        }


def compute_valuation(
    our_eps: float,
    ticker: str,
    consensus_eps: float = None,
    verbose: bool = False,
) -> ValuationAssessment | None:
    """
    Compute multiple-based valuation.

    Uses forward PE from yfinance as the base multiple. This represents
    what the market currently pays per dollar of forward earnings.

    Our implied price = our_eps * forward_pe
    If our EPS > consensus EPS, implied price > current price.
    """
    def v(msg):
        if verbose:
            print(msg)

    try:
        import yfinance as yf
        t = yf.Ticker(ticker.upper())
        info = t.info
    except Exception as e:
        v(f"  Valuation: yfinance error - {e}")
        return None

    current_price = info.get("currentPrice") or info.get("regularMarketPrice")
    forward_pe = info.get("forwardPE")
    trailing_pe = info.get("trailingPE")

    if not current_price:
        v(f"  Valuation: no price data")
        return None

    # Choose multiple
    if forward_pe and forward_pe > 0 and forward_pe < 200:
        applied_pe = forward_pe
        source = "forward_pe"
    elif trailing_pe and trailing_pe > 0 and trailing_pe < 200:
        applied_pe = trailing_pe
        source = "trailing_pe"
    else:
        v(f"  Valuation: no usable PE ratio")
        return None

    # For negative EPS companies, PE-based valuation doesn't work
    if our_eps <= 0:
        v(f"  Valuation: negative EPS (${our_eps:.2f}), skipping PE valuation")
        return ValuationAssessment(
            ticker=ticker,
            current_price=current_price,
            narrative=f"Negative EPS (${our_eps:.2f}) -- PE valuation not applicable. "
                      f"Current price: ${current_price:.2f}.",
        )

    # Core valuation
    implied_price = our_eps * applied_pe
    upside_pct = (implied_price / current_price - 1) * 100

    # Sensitivity: +/- 3 PE turns
    pe_low = max(applied_pe - 3, applied_pe * 0.8)
    pe_high = applied_pe + 3
    price_low = our_eps * pe_low
    price_high = our_eps * pe_high
    upside_low = (price_low / current_price - 1) * 100
    upside_high = (price_high / current_price - 1) * 100

    # Consensus implied price
    cons_implied = consensus_eps * applied_pe if consensus_eps else 0

    # Narrative
    direction = "upside" if upside_pct > 0 else "downside"
    parts = [
        f"At {applied_pe:.1f}x {source.replace('_', ' ')}, "
        f"our ${our_eps:.2f} EPS implies ${implied_price:.2f} "
        f"({abs(upside_pct):.1f}% {direction} from ${current_price:.2f}).",
    ]
    if consensus_eps and abs(our_eps - consensus_eps) > 0.01:
        cons_direction = "above" if our_eps > consensus_eps else "below"
        parts.append(
            f"Our EPS is ${abs(our_eps - consensus_eps):.2f} {cons_direction} consensus "
            f"(${consensus_eps:.2f}), implying ${abs(implied_price - cons_implied):.2f} "
            f"of price differential.")
    parts.append(
        f"Sensitivity: ${price_low:.2f} at {pe_low:.1f}x to "
        f"${price_high:.2f} at {pe_high:.1f}x.")

    assessment = ValuationAssessment(
        ticker=ticker,
        implied_price=implied_price,
        current_price=current_price,
        upside_pct=upside_pct,
        applied_multiple=applied_pe,
        multiple_source=source,
        multiple_low=pe_low,
        multiple_high=pe_high,
        price_at_low=price_low,
        price_at_high=price_high,
        upside_at_low=upside_low,
        upside_at_high=upside_high,
        trailing_pe=trailing_pe or 0,
        forward_pe=forward_pe or 0,
        consensus_implied_price=cons_implied,
        narrative=" ".join(parts),
    )

    v(f"  Valuation: ${implied_price:.2f} ({upside_pct:+.1f}%) at {applied_pe:.1f}x PE")
    return assessment





# ═══════════════════════════════════════════════════════════════
# DCF Valuation
# ═══════════════════════════════════════════════════════════════

@dataclass
class DCFAssessment:
    """Result of DCF valuation."""
    ticker: str = ""
    # WACC inputs
    risk_free_rate: float = 0
    equity_risk_premium: float = 0
    beta: float = 0
    cost_of_equity: float = 0
    debt_pct: float = 0
    cost_of_debt: float = 0
    tax_rate: float = 0
    wacc: float = 0
    # Terminal value
    terminal_growth_rate: float = 0
    exit_multiple: float = 0
    terminal_value_perpetuity: float = 0
    terminal_value_exit: float = 0
    # Valuation
    pv_fcfs: float = 0
    pv_terminal_perpetuity: float = 0
    pv_terminal_exit: float = 0
    enterprise_value_perpetuity: float = 0
    enterprise_value_exit: float = 0
    equity_value_perpetuity: float = 0
    equity_value_exit: float = 0
    implied_price_perpetuity: float = 0
    implied_price_exit: float = 0
    shares_outstanding: float = 0
    net_debt: float = 0
    # Context
    current_price: float = 0
    upside_perpetuity: float = 0
    upside_exit: float = 0
    fcf_projections: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "wacc": {
                "risk_free_rate": round(self.risk_free_rate, 4),
                "equity_risk_premium": round(self.equity_risk_premium, 4),
                "beta": round(self.beta, 2),
                "cost_of_equity": round(self.cost_of_equity, 4),
                "wacc": round(self.wacc, 4),
            },
            "terminal": {
                "growth_rate": round(self.terminal_growth_rate, 4),
                "exit_multiple": round(self.exit_multiple, 1),
                "tv_perpetuity": round(self.terminal_value_perpetuity, 1),
                "tv_exit": round(self.terminal_value_exit, 1),
            },
            "valuation": {
                "ev_perpetuity": round(self.enterprise_value_perpetuity, 1),
                "ev_exit": round(self.enterprise_value_exit, 1),
                "implied_price_perpetuity": round(self.implied_price_perpetuity, 2),
                "implied_price_exit": round(self.implied_price_exit, 2),
                "upside_perpetuity": round(self.upside_perpetuity, 1),
                "upside_exit": round(self.upside_exit, 1),
            },
        }


def compute_dcf(
    fcf_projections: list[float],
    terminal_year_fcf: float,
    wacc_inputs: dict,
    shares: float,
    net_debt: float = 0,
    current_price: float = None,
    ticker: str = "",
    verbose: bool = False,
) -> DCFAssessment:
    """
    Compute DCF valuation with both perpetuity growth and exit multiple methods.

    Args:
        fcf_projections: List of annual UFCF values (e.g., 5 years)
        terminal_year_fcf: Last year FCF for terminal value
        wacc_inputs: Dict with rf, erp, beta, debt_pct, cost_of_debt, tax_rate
        shares: Diluted shares outstanding (millions)
        net_debt: Net debt (debt - cash), negative if net cash
        current_price: Current stock price for upside calc
        ticker: Company ticker
    """
    def v(msg):
        if verbose:
            print(msg)

    # WACC calculation (CAPM)
    rf = wacc_inputs.get("rf", 0.043)
    erp = wacc_inputs.get("erp", 0.055)
    beta = wacc_inputs.get("beta", 1.0)
    debt_pct = wacc_inputs.get("debt_pct", 0)
    cost_of_debt = wacc_inputs.get("cost_of_debt", 0.05)
    tax_rate = wacc_inputs.get("tax_rate", 0.26)
    tgr = wacc_inputs.get("terminal_growth_rate", 0.025)
    exit_mult = wacc_inputs.get("exit_multiple", 20)

    cost_of_equity = rf + beta * erp
    equity_pct = 1 - debt_pct
    after_tax_debt = cost_of_debt * (1 - tax_rate)
    wacc = cost_of_equity * equity_pct + after_tax_debt * debt_pct

    v(f"  DCF: Ke={cost_of_equity:.1%}, WACC={wacc:.1%}")

    # PV of projected FCFs
    pv_fcfs = 0
    for i, fcf in enumerate(fcf_projections):
        pv_fcfs += fcf / (1 + wacc) ** (i + 1)

    # Terminal value — perpetuity growth
    tv_perpetuity = terminal_year_fcf * (1 + tgr) / (wacc - tgr)
    n = len(fcf_projections)
    pv_tv_perpetuity = tv_perpetuity / (1 + wacc) ** n

    # Terminal value — exit multiple (on EBITDA proxy: FCF * multiple)
    tv_exit = terminal_year_fcf * exit_mult
    pv_tv_exit = tv_exit / (1 + wacc) ** n

    # Enterprise value → equity value → price per share
    ev_perp = pv_fcfs + pv_tv_perpetuity
    ev_exit = pv_fcfs + pv_tv_exit
    eq_perp = ev_perp - net_debt
    eq_exit = ev_exit - net_debt
    price_perp = eq_perp / shares if shares > 0 else 0
    price_exit = eq_exit / shares if shares > 0 else 0

    upside_perp = ((price_perp / current_price) - 1) * 100 if current_price else 0
    upside_exit = ((price_exit / current_price) - 1) * 100 if current_price else 0

    v(f"  DCF: Perpetuity=${price_perp:.2f} ({upside_perp:+.1f}%), Exit=${price_exit:.2f} ({upside_exit:+.1f}%)")

    return DCFAssessment(
        ticker=ticker,
        risk_free_rate=rf, equity_risk_premium=erp, beta=beta,
        cost_of_equity=cost_of_equity, debt_pct=debt_pct,
        cost_of_debt=cost_of_debt, tax_rate=tax_rate, wacc=wacc,
        terminal_growth_rate=tgr, exit_multiple=exit_mult,
        terminal_value_perpetuity=tv_perpetuity, terminal_value_exit=tv_exit,
        pv_fcfs=pv_fcfs, pv_terminal_perpetuity=pv_tv_perpetuity,
        pv_terminal_exit=pv_tv_exit,
        enterprise_value_perpetuity=ev_perp, enterprise_value_exit=ev_exit,
        equity_value_perpetuity=eq_perp, equity_value_exit=eq_exit,
        implied_price_perpetuity=price_perp, implied_price_exit=price_exit,
        shares_outstanding=shares, net_debt=net_debt,
        current_price=current_price or 0,
        upside_perpetuity=upside_perp, upside_exit=upside_exit,
        fcf_projections=fcf_projections,
    )


# ═══════════════════════════════════════════════════════════════
# Comparable Companies
# ═══════════════════════════════════════════════════════════════

@dataclass
class CompanyComp:
    """A single comparable company's metrics."""
    name: str = ""
    ticker: str = ""
    mkt_cap: float = 0      # $B
    ev: float = 0            # $B
    ev_ebitda: float = 0     # NTM
    pe: float = 0            # NTM
    ev_rev: float = 0        # NTM
    rev_growth: float = 0    # %
    ebitda_margin: float = 0 # %
    net_margin: float = 0    # %


def fetch_comps(tickers: list[str], verbose: bool = False) -> list[CompanyComp]:
    """
    Fetch comparable company metrics via yfinance.

    Args:
        tickers: List of ticker symbols
        verbose: Print progress

    Returns:
        List of CompanyComp objects
    """
    try:
        import yfinance as yf
    except ImportError:
        return []

    comps = []
    for t in tickers:
        try:
            stock = yf.Ticker(t)
            info = stock.info

            mkt_cap = (info.get("marketCap", 0) or 0) / 1e9
            ev_val = (info.get("enterpriseValue", 0) or 0) / 1e9
            fwd_pe = info.get("forwardPE", 0) or 0
            ev_ebitda = info.get("enterpriseToEbitda", 0) or 0
            ev_rev = info.get("enterpriseToRevenue", 0) or 0
            rev_growth = (info.get("revenueGrowth", 0) or 0) * 100
            profit_margin = (info.get("profitMargins", 0) or 0) * 100
            ebitda_margin = (info.get("ebitdaMargins", 0) or 0) * 100

            comp = CompanyComp(
                name=info.get("shortName", t),
                ticker=t,
                mkt_cap=round(mkt_cap, 1),
                ev=round(ev_val, 1),
                ev_ebitda=round(ev_ebitda, 1),
                pe=round(fwd_pe, 1),
                ev_rev=round(ev_rev, 1),
                rev_growth=round(rev_growth, 1),
                ebitda_margin=round(ebitda_margin, 1),
                net_margin=round(profit_margin, 1),
            )
            comps.append(comp)
            if verbose:
                print(f"  Comp: {t} PE={fwd_pe:.1f}x EV/EBITDA={ev_ebitda:.1f}x")

        except Exception as e:
            if verbose:
                print(f"  Comp {t} failed: {e}")

    return comps
