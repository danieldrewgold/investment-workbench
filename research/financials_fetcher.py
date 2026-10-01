"""
Unified Financials Fetcher

Orchestrates: Polygon → Alpha Vantage → company_registry fallback.

Produces a StructuredFinancials object that the pipeline feeds directly
to the Claude API call (qualitative reasoning only — no number extraction
needed). Also produces the prior_year / constants dicts that ModelSpec needs.

The key insight: structured APIs give us exact numbers, so Claude only
needs to do business understanding, driver selection, and contradiction
generation — not number extraction.
"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class StructuredFinancials:
    """
    Normalized financial data from any source.
    This is what the pipeline works with after fetching.
    """
    source: str = ""                    # "polygon", "alpha_vantage", "registry"
    ticker: str = ""
    fiscal_year: int = 0
    fiscal_period: str = "FY"

    # Income statement (in millions)
    revenue_m: float = 0
    cost_of_revenue_m: float = 0
    gross_profit_m: float = 0
    operating_income_m: float = 0
    sga_m: float = 0
    rd_m: float = 0
    da_m: float = 0
    interest_expense_m: float = 0
    interest_income_m: float = 0
    net_interest_m: float = 0           # positive = income, negative = expense
    pretax_income_m: float = 0
    tax_expense_m: float = 0
    net_income_m: float = 0
    diluted_eps: float = 0
    diluted_shares_m: float = 0

    # Percentages (as % of revenue)
    gross_margin_pct: float = 0
    cost_of_revenue_pct: float = 0
    sga_pct: float = 0
    rd_pct: float = 0
    operating_margin_pct: float = 0
    tax_rate: float = 0

    # Flags
    has_detailed_costs: bool = False     # whether we have granular cost breakdown
    has_per_share: bool = False          # whether EPS/shares are available

    # Currency — default USD. For foreign reporters (SONY reports JPY, TM
    # reports JPY, BABA reports CNY, etc.) we convert to USD at fetch time
    # so downstream math compares cleanly against yfinance's USD consensus.
    currency: str = "USD"                # currency of the values in this object
    original_currency: str = "USD"        # reporting currency (before any FX conversion)
    fx_rate_applied: float = 1.0         # FX multiplier used (original → USD)

    def to_summary_text(self) -> str:
        """
        Produce a compact text summary of financials for the Claude prompt.
        This gives Claude structured numbers without it needing to extract them.
        """
        lines = [
            f"STRUCTURED FINANCIALS ({self.source}, FY{self.fiscal_year}):",
            f"  Revenue: ${self.revenue_m:,.1f}M",
            f"  Cost of Revenue: ${self.cost_of_revenue_m:,.1f}M ({self.cost_of_revenue_pct:.1f}% of rev)",
            f"  Gross Profit: ${self.gross_profit_m:,.1f}M ({self.gross_margin_pct:.1f}% margin)",
        ]
        if self.sga_m:
            lines.append(f"  SG&A: ${self.sga_m:,.1f}M ({self.sga_pct:.1f}% of rev)")
        if self.rd_m:
            lines.append(f"  R&D: ${self.rd_m:,.1f}M ({self.rd_pct:.1f}% of rev)")
        if self.da_m:
            lines.append(f"  D&A: ${self.da_m:,.1f}M")
        lines.append(f"  Operating Income: ${self.operating_income_m:,.1f}M ({self.operating_margin_pct:.1f}% margin)")
        if self.net_interest_m:
            label = "Income" if self.net_interest_m > 0 else "Expense"
            lines.append(f"  Net Interest {label}: ${abs(self.net_interest_m):,.1f}M")
        lines.append(f"  Tax Rate: {self.tax_rate*100:.1f}%")
        lines.append(f"  Net Income: ${self.net_income_m:,.1f}M")
        if self.has_per_share:
            lines.append(f"  Diluted EPS: ${self.diluted_eps:.2f}")
            lines.append(f"  Diluted Shares: {self.diluted_shares_m:,.1f}M")
        return "\n".join(lines)


def fetch_quarterly_financials(ticker: str, verbose: bool = False) -> StructuredFinancials | None:
    """
    Fetch the most recent quarterly financials for a ticker.
    Returns StructuredFinancials for the latest quarter, or None.
    """
    def v(msg):
        if verbose:
            print(msg)

    try:
        from ingestion.loaders.polygon_financials import fetch_polygon_financials, _parse_filing
        import httpx
        from ingestion.loaders.polygon_financials import POLYGON_API_KEY, BASE_URL

        resp = httpx.get(f"{BASE_URL}/vX/reference/financials", params={
            "ticker": ticker.upper(), "timeframe": "quarterly", "limit": 1,
            "apiKey": POLYGON_API_KEY,
        }, timeout=15.0)

        if resp.status_code != 200:
            v(f"  Quarterly: Polygon returned {resp.status_code}")
            return None

        results = resp.json().get("results", [])
        if not results:
            v(f"  Quarterly: no data")
            return None

        parsed = _parse_filing(results[0])
        ic = parsed.get("income_statement", {})
        pcts = parsed.get("percentages", {})

        sf = StructuredFinancials(
            source="polygon_quarterly",
            ticker=ticker,
            fiscal_year=parsed.get("fiscal_year", 0),
            fiscal_period=parsed.get("fiscal_period", "Q?"),
            revenue_m=ic.get("revenue_m", 0),
            cost_of_revenue_m=round(ic.get("cost_of_revenue", 0) / 1e6, 1) if ic.get("cost_of_revenue") else 0,
            gross_profit_m=round(ic.get("gross_profit", 0) / 1e6, 1) if ic.get("gross_profit") else 0,
            operating_income_m=round(ic.get("operating_income", 0) / 1e6, 1) if ic.get("operating_income") else 0,
            sga_m=round(ic.get("sga", 0) / 1e6, 1) if ic.get("sga") else 0,
            rd_m=round(ic.get("research_and_development", 0) / 1e6, 1) if ic.get("research_and_development") else 0,
            da_m=round(ic.get("depreciation_amortization", 0) / 1e6, 1) if ic.get("depreciation_amortization") else 0,
            net_income_m=round(ic.get("net_income", 0) / 1e6, 1) if ic.get("net_income") else 0,
            diluted_eps=ic.get("diluted_eps", 0),
            diluted_shares_m=round(ic.get("diluted_shares", 0) / 1e6, 1) if ic.get("diluted_shares") else 0,
            gross_margin_pct=pcts.get("gross_margin_pct", 0),
            cost_of_revenue_pct=pcts.get("cost_of_revenue_pct", 0),
            operating_margin_pct=pcts.get("operating_margin_pct", 0),
            tax_rate=parsed.get("tax_rate", 0),
            has_per_share=bool(ic.get("diluted_eps")),
        )

        # Compute net interest
        if sf.operating_income_m:
            pretax = round(ic.get("pretax_income", 0) / 1e6, 1) if ic.get("pretax_income") else 0
            if pretax:
                sf.net_interest_m = round(pretax - sf.operating_income_m, 1)
                sf.pretax_income_m = pretax

        v(f"  Quarterly: {sf.fiscal_period} FY{sf.fiscal_year} rev=${sf.revenue_m:,.1f}M eps=${sf.diluted_eps:.2f}")
        return sf

    except Exception as e:
        v(f"  Quarterly: error - {e}")
        return None


def _try_yfinance(ticker: str, v=lambda _: None) -> "StructuredFinancials | None":
    """Income-statement fallback for international / thinly-covered names — foreign
    ADRs like JBFCY (Jollibee) that Polygon/AV don't carry but yfinance does.
    Pulls Revenue / Operating Income / Net Income from the annual income
    statement. EPS is deliberately left blank: yfinance's per-share figure is
    unreliable for ADRs (share-ratio + currency confusion — JBFCY reads 620 / 37k).
    The fundamental trajectory (scale, margins, NI) is what we trust;
    `_convert_to_usd` handles the reporting currency (e.g. PHP→USD)."""
    try:
        import yfinance as yf
        a = yf.Ticker(ticker).income_stmt
        if a is None or a.empty:
            return None
        col = a.columns[0]

        def g(*rows):
            for r in rows:
                if r in a.index:
                    try:
                        x = float(a.loc[r, col])
                        if x == x:  # not NaN
                            return x
                    except Exception:
                        pass
            return 0.0

        rev = g("Total Revenue", "Operating Revenue")
        if not rev or rev <= 0:
            return None
        op_inc = g("Operating Income")
        ni = g("Net Income", "Net Income Common Stockholders")
        cogs = g("Cost Of Revenue")
        sf = StructuredFinancials(
            source="yfinance",
            ticker=ticker.upper(),
            fiscal_year=getattr(col, "year", 0) or 0,
            fiscal_period="FY",
            revenue_m=round(rev / 1e6, 1),
            cost_of_revenue_m=round(cogs / 1e6, 1) if cogs else 0,
            operating_income_m=round(op_inc / 1e6, 1) if op_inc else 0,
            net_income_m=round(ni / 1e6, 1) if ni else 0,
            diluted_eps=0,          # ADR per-share EPS is unreliable — don't fabricate
            has_per_share=False,
            # Margins are currency-neutral ratios — set them so the brief doesn't
            # read "0.0% margin" (conversion scales $ fields but not percentages).
            operating_margin_pct=round(op_inc / rev * 100, 1) if op_inc else 0,
            cost_of_revenue_pct=round(cogs / rev * 100, 1) if cogs else 0,
            gross_margin_pct=round((rev - cogs) / rev * 100, 1) if cogs else 0,
        )
        v(f"  yfinance fallback: FY{sf.fiscal_year} rev={sf.revenue_m:,.0f}M "
          f"opinc={sf.operating_income_m:,.0f}M NI={sf.net_income_m:,.0f}M (pre-FX)")
        return sf
    except Exception as e:
        v(f"  yfinance fallback failed: {type(e).__name__}: {e}")
        return None


def _ensure_adr_eps(fin: "StructuredFinancials", v=lambda _: None) -> "StructuredFinancials":
    """Backfill a missing per-share EPS (USD) from yfinance for foreign ADRs where
    Polygon/AV don't carry one. yfinance `trailingEps` is in the ADR's USD trading
    terms WHEN VALID, so this runs AFTER any FX conversion. But yfinance frequently
    reports a native-currency / wrong-share-ratio EPS for thin ADRs (e.g. JBFCY
    trailingEps=617 on a ~$35 ADR), so we accept it only when the implied trailing
    P/E against the ADR's own price is sane. TTM-based — a reasonable historical
    anchor when the filing EPS is unavailable (vs a misleading $0.00)."""
    if fin.diluted_eps:        # already have a usable per-share figure
        return fin
    info = _fetch_yfinance_info(fin.ticker)
    try:
        eps = float(info.get("trailingEps"))
    except (TypeError, ValueError):
        return fin
    if not eps or eps != eps:  # zero or NaN
        return fin
    # Validate the per-share scale against the ADR's trading price — this is what
    # catches native-currency EPS (a $617 "EPS" on a $35 ADR is not real USD/ADR).
    price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        return fin             # can't validate the scale — safer to leave blank
    sane = (1.0 <= price / eps <= 200.0) if eps > 0 else (abs(eps) <= price)
    if not sane:
        v(f"  EPS backfill skipped: yfinance trailingEps ${eps:.2f} vs ADR price "
          f"${price:.2f} looks native-currency, not USD/ADR")
        return fin
    fin.diluted_eps = round(eps, 2)
    fin.has_per_share = True
    v(f"  EPS: backfilled from yfinance trailingEps ${fin.diluted_eps:.2f}/ADR (TTM)")
    return fin


def fetch_financials(ticker: str, registry_data: dict = None, verbose: bool = False) -> StructuredFinancials:
    """
    Fetch structured financials: Polygon + Alpha Vantage → registry fallback.

    Strategy: Polygon has best EPS/shares data but often lacks granular cost
    breakdown. Alpha Vantage has SGA, R&D, D&A, interest, EBITDA. We try
    both and merge the best fields from each.

    Falls back to registry cache only if both APIs fail.
    """
    def v(msg):
        if verbose:
            print(msg)

    polygon_result = _try_polygon(ticker, v)
    av_result = _try_alpha_vantage(ticker, v)

    result = None
    if polygon_result and av_result:
        # Merge: use Polygon for top-line + EPS, AV for cost detail
        result = _merge_sources(polygon_result, av_result, ticker=ticker, v=v)
        v(f"  Financials: merged Polygon + Alpha Vantage")
    elif polygon_result:
        # Even single-source: reconcile shares against yfinance for dual-class coverage
        polygon_result.diluted_shares_m = _reconcile_shares(
            ticker, poly_shares_m=polygon_result.diluted_shares_m, av_shares_m=0, v=v)
        result = polygon_result
    elif av_result:
        av_result.diluted_shares_m = _reconcile_shares(
            ticker, poly_shares_m=0, av_shares_m=av_result.diluted_shares_m, v=v)
        result = av_result

    if result is None:
        # International / thinly-covered names (foreign ADRs like JBFCY) often aren't
        # in Polygon/AV but ARE in yfinance — pull the income statement there before
        # giving up. _convert_to_usd handles the reporting currency (native → USD).
        result = _try_yfinance(ticker, v)
        if result:
            v("  Financials: yfinance income-statement fallback")

    if result is not None:
        # Normalize to USD (no-op for Polygon/AV — already USD; converts the
        # yfinance native-currency fallback), then backfill a per-ADR EPS from
        # yfinance for foreign names where the providers left it blank.
        result = _convert_to_usd(result, v=v)
        result = _ensure_adr_eps(result, v=v)
        return result

    # Final fallback: registry cache
    if registry_data:
        v("  Financials: using registry cache (APIs unavailable)")
        return _from_registry(ticker, registry_data)

    v("  Financials: no data source available")
    return StructuredFinancials(source="none", ticker=ticker)


# --------------------------------------------------------------------------
# yfinance-backed reconcilers
# --------------------------------------------------------------------------

def _fetch_yfinance_info(ticker: str):
    """Cached getter for yfinance ticker info — avoids repeated network calls."""
    try:
        import yfinance as yf
        return yf.Ticker(ticker).info or {}
    except Exception:
        return {}


def _reconcile_shares(ticker: str, *, poly_shares_m: float, av_shares_m: float,
                      v=lambda _: None) -> float:
    """
    Return diluted share count, correcting for dual-class companies.

    Polygon and Alpha Vantage often report diluted shares from one share
    class only (commonly Class A). For dual-class firms (WMG, GOOGL, NFLX,
    META, etc.) this UNDERSTATES total shares ~5-6x — causing EPS to be
    overstated by the same factor.

    yfinance's `impliedSharesOutstanding` is derived from market cap / price
    and therefore aggregates ALL share classes. When it materially exceeds
    Polygon/AV shares, we trust yfinance (dual-class case). When they're
    close, we trust Polygon/AV (typically more precise for diluted count).
    """
    poly_or_av = poly_shares_m or av_shares_m or 0.0
    info = _fetch_yfinance_info(ticker)
    yf_basic = info.get("sharesOutstanding") or 0
    yf_implied = info.get("impliedSharesOutstanding") or 0
    yf_m = max(yf_basic, yf_implied) / 1e6 if max(yf_basic, yf_implied) else 0

    if poly_or_av == 0 and yf_m > 0:
        v(f"  Shares: using yfinance {yf_m:.1f}M (Polygon/AV missing)")
        return round(yf_m, 1)
    if yf_m == 0:
        return poly_or_av
    # If yfinance disagrees by >25%, it's likely the dual-class case —
    # Polygon/AV reported one class only. Prefer yfinance.
    if yf_m > poly_or_av * 1.25:
        v(f"  Shares: yfinance {yf_m:.1f}M >> Polygon/AV {poly_or_av:.1f}M "
          f"(likely dual-class — using yfinance)")
        return round(yf_m, 1)
    # Otherwise Polygon/AV is fine (and usually more precise for diluted count)
    return poly_or_av


def _detect_financial_currency(ticker: str) -> str:
    """
    Return the currency the company reports in (e.g., 'USD', 'JPY', 'EUR').

    For US-listed ADRs of foreign companies (SONY, TM, BABA, etc.),
    yfinance's `info['financialCurrency']` is the COMPANY'S reporting
    currency, which may differ from the ADR's trading currency.

    We use this to detect when native financials need FX conversion to
    USD for apples-to-apples comparison against USD consensus estimates.
    """
    info = _fetch_yfinance_info(ticker)
    return (info.get("financialCurrency") or info.get("currency") or "USD").upper()


_FX_CACHE: dict[str, float] = {}


def _fetch_fx_rate(from_ccy: str, to_ccy: str = "USD") -> float | None:
    """
    Return the FX rate (1 from_ccy = X to_ccy) via yfinance.

    yfinance uses ticker symbols like 'JPYUSD=X' for FX pairs. For JPY→USD
    we need the inverse pair because JPYUSD=X gives us what 1 JPY is worth
    in USD (e.g., ~0.0067).

    Cached per session since FX rates move slowly relative to a research
    session's duration.
    """
    if from_ccy == to_ccy:
        return 1.0
    key = f"{from_ccy}{to_ccy}"
    if key in _FX_CACHE:
        return _FX_CACHE[key]
    try:
        import yfinance as yf
        # yfinance FX pair symbol format: "{from}{to}=X"
        symbol = f"{from_ccy}{to_ccy}=X"
        fx_hist = yf.Ticker(symbol).history(period="5d")
        if fx_hist is None or fx_hist.empty:
            return None
        rate = float(fx_hist["Close"].iloc[-1])
        if rate <= 0 or rate != rate:  # guard against NaN
            return None
        _FX_CACHE[key] = rate
        return rate
    except Exception:
        return None


def _convert_to_usd(fin: StructuredFinancials, v=lambda _: None) -> StructuredFinancials:
    """
    If the financials are in a non-USD reporting currency, convert all
    monetary fields in place to USD using current FX rate.

    Per-share values (diluted_eps) also get converted. Percentages stay.
    The `currency`, `original_currency`, and `fx_rate_applied` fields are
    updated so downstream code + reports can see what happened.
    """
    # Polygon and Alpha Vantage normalize US-listed ADRs to USD already; only the
    # yfinance income-statement fallback pulls native-currency statements. FX-
    # converting Polygon/AV data DOUBLE-converts it (e.g. FMX: AV reports $46.6B
    # USD; applying MXN→USD would shrink it ~17x to $2.7B). Restrict conversion to
    # the yfinance-sourced path — for everything else the values are already USD.
    if "yfinance" not in (fin.source or ""):
        fin.currency = "USD"
        fin.original_currency = "USD"
        fin.fx_rate_applied = 1.0
        return fin

    native_ccy = _detect_financial_currency(fin.ticker)
    if native_ccy == "USD":
        fin.currency = "USD"
        fin.original_currency = "USD"
        fin.fx_rate_applied = 1.0
        return fin

    fx_rate = _fetch_fx_rate(native_ccy, "USD")
    if fx_rate is None:
        v(f"  Currency: reports {native_ccy} but FX rate unavailable — leaving native")
        fin.currency = native_ccy
        fin.original_currency = native_ccy
        fin.fx_rate_applied = 1.0
        return fin

    v(f"  Currency: converting {native_ccy} -> USD at rate {fx_rate:.6f}")

    # Monetary fields to scale
    monetary_attrs = [
        "revenue_m", "cost_of_revenue_m", "gross_profit_m",
        "operating_income_m", "sga_m", "rd_m", "da_m",
        "interest_expense_m", "interest_income_m", "net_interest_m",
        "pretax_income_m", "tax_expense_m", "net_income_m",
        "diluted_eps",
    ]
    for attr in monetary_attrs:
        val = getattr(fin, attr, None)
        if val:
            setattr(fin, attr, round(val * fx_rate, 4))

    fin.currency = "USD"
    fin.original_currency = native_ccy
    fin.fx_rate_applied = fx_rate
    return fin


def _merge_sources(poly: StructuredFinancials, av: StructuredFinancials,
                    *, ticker: str = "", v=lambda _: None) -> StructuredFinancials:
    """
    Merge Polygon and Alpha Vantage data, taking the best from each.
    Polygon: revenue, operating income, EPS, shares (most accurate for recent filings)
    Alpha Vantage: cost detail (SGA, R&D, D&A, interest, gross profit)
    """
    merged = StructuredFinancials(
        source="polygon+alpha_vantage",
        ticker=poly.ticker,
        fiscal_year=poly.fiscal_year or av.fiscal_year,
        fiscal_period=poly.fiscal_period,
        # Revenue: prefer Polygon
        revenue_m=poly.revenue_m or av.revenue_m,
        # Cost detail: prefer AV (Polygon often missing)
        cost_of_revenue_m=av.cost_of_revenue_m or poly.cost_of_revenue_m,
        gross_profit_m=av.gross_profit_m or poly.gross_profit_m,
        # Operating income: prefer Polygon
        operating_income_m=poly.operating_income_m or av.operating_income_m,
        # Cost breakdown: from AV
        sga_m=av.sga_m or poly.sga_m,
        rd_m=av.rd_m or poly.rd_m,
        da_m=av.da_m or poly.da_m,
        interest_expense_m=av.interest_expense_m or poly.interest_expense_m,
        interest_income_m=av.interest_income_m,
        net_interest_m=av.net_interest_m or poly.net_interest_m,
        # Bottom line: prefer Polygon
        pretax_income_m=poly.pretax_income_m or av.pretax_income_m,
        tax_expense_m=poly.tax_expense_m or av.tax_expense_m,
        net_income_m=poly.net_income_m or av.net_income_m,
        # EPS/shares: prefer Polygon
        diluted_eps=poly.diluted_eps or av.diluted_eps,
        diluted_shares_m=_reconcile_shares(
            ticker,
            poly_shares_m=poly.diluted_shares_m,
            av_shares_m=av.diluted_shares_m,
            v=v,
        ),
        # Percentages: recompute from best numbers
        tax_rate=poly.tax_rate or av.tax_rate,
        has_detailed_costs=av.has_detailed_costs or poly.has_detailed_costs,
        has_per_share=poly.has_per_share or av.has_per_share,
    )

    # Recompute percentages from merged numbers
    rev = merged.revenue_m
    if rev and rev > 0:
        if merged.cost_of_revenue_m:
            merged.cost_of_revenue_pct = round(merged.cost_of_revenue_m / rev * 100, 1)
        if merged.gross_profit_m:
            merged.gross_margin_pct = round(merged.gross_profit_m / rev * 100, 1)
        elif merged.cost_of_revenue_m:
            merged.gross_profit_m = round(rev - merged.cost_of_revenue_m, 1)
            merged.gross_margin_pct = round(merged.gross_profit_m / rev * 100, 1)
        if merged.sga_m:
            merged.sga_pct = round(merged.sga_m / rev * 100, 1)
        if merged.rd_m:
            merged.rd_pct = round(merged.rd_m / rev * 100, 1)
        if merged.operating_income_m:
            merged.operating_margin_pct = round(merged.operating_income_m / rev * 100, 1)

    return merged


def _try_polygon(ticker: str, v) -> StructuredFinancials | None:
    """Try fetching from Polygon."""
    try:
        from ingestion.loaders.polygon_financials import fetch_polygon_financials
        data = fetch_polygon_financials(ticker, timeframe="annual", limit=1)
        if not data:
            v("  Polygon financials: no data")
            return None

        ic = data.get("income_statement", {})
        pcts = data.get("percentages", {})

        sf = StructuredFinancials(
            source="polygon",
            ticker=ticker,
            fiscal_year=data.get("fiscal_year", 0),
            fiscal_period=data.get("fiscal_period", "FY"),
            revenue_m=ic.get("revenue_m", 0),
            cost_of_revenue_m=round(ic.get("cost_of_revenue", 0) / 1e6, 1) if ic.get("cost_of_revenue") else 0,
            gross_profit_m=round(ic.get("gross_profit", 0) / 1e6, 1) if ic.get("gross_profit") else 0,
            operating_income_m=round(ic.get("operating_income", 0) / 1e6, 1) if ic.get("operating_income") else 0,
            sga_m=round(ic.get("sga", 0) / 1e6, 1) if ic.get("sga") else 0,
            rd_m=round(ic.get("research_and_development", 0) / 1e6, 1) if ic.get("research_and_development") else 0,
            da_m=round(ic.get("depreciation_amortization", 0) / 1e6, 1) if ic.get("depreciation_amortization") else 0,
            interest_expense_m=round(ic.get("interest_expense", 0) / 1e6, 1) if ic.get("interest_expense") else 0,
            pretax_income_m=round(ic.get("pretax_income", 0) / 1e6, 1) if ic.get("pretax_income") else 0,
            tax_expense_m=round(ic.get("tax_expense", 0) / 1e6, 1) if ic.get("tax_expense") else 0,
            net_income_m=round(ic.get("net_income", 0) / 1e6, 1) if ic.get("net_income") else 0,
            diluted_eps=ic.get("diluted_eps", 0),
            diluted_shares_m=round(ic.get("diluted_shares", 0) / 1e6, 1) if ic.get("diluted_shares") else 0,
            gross_margin_pct=pcts.get("gross_margin_pct", 0),
            cost_of_revenue_pct=pcts.get("cost_of_revenue_pct", 0),
            sga_pct=pcts.get("sga_pct", 0),
            rd_pct=pcts.get("rd_pct", 0),
            operating_margin_pct=pcts.get("operating_margin_pct", 0),
            tax_rate=data.get("tax_rate", 0),
            has_detailed_costs=bool(ic.get("sga")),
            has_per_share=bool(ic.get("diluted_eps")),
        )

        # Compute net interest if not directly available
        if ic.get("interest_expense"):
            sf.net_interest_m = -sf.interest_expense_m
        # Some companies have net interest income (e.g., from cash balances)
        # Polygon doesn't always separate this, so we infer from pretax - operating
        if sf.pretax_income_m and sf.operating_income_m:
            implied_interest = sf.pretax_income_m - sf.operating_income_m
            if abs(implied_interest) > 0.5:
                sf.net_interest_m = round(implied_interest, 1)

        v(f"  Polygon financials: FY{sf.fiscal_year} rev=${sf.revenue_m:,.1f}M eps=${sf.diluted_eps:.2f}")
        return sf

    except Exception as e:
        v(f"  Polygon financials: error - {e}")
        return None


def _try_alpha_vantage(ticker: str, v) -> StructuredFinancials | None:
    """Try fetching from Alpha Vantage."""
    try:
        from ingestion.loaders.alpha_vantage_financials import fetch_av_financials, fetch_av_overview
        data = fetch_av_financials(ticker)
        if not data:
            v("  Alpha Vantage financials: no data")
            return None

        ic = data.get("income_statement", {})
        pcts = data.get("percentages", {})

        # Get per-share data from overview endpoint
        overview = fetch_av_overview(ticker)
        diluted_eps = 0
        diluted_shares_m = 0
        if overview:
            diluted_eps = overview.get("diluted_eps_ttm", 0)
            shares = overview.get("shares_outstanding", 0)
            diluted_shares_m = round(shares / 1e6, 1) if shares else 0

        # Compute net interest
        net_interest = ic.get("net_interest", 0)
        if not net_interest:
            net_interest = (ic.get("interest_income", 0) or 0) - (ic.get("interest_expense", 0) or 0)

        sf = StructuredFinancials(
            source="alpha_vantage",
            ticker=ticker,
            fiscal_year=data.get("fiscal_year", 0),
            fiscal_period="FY",
            revenue_m=ic.get("revenue_m", 0),
            cost_of_revenue_m=round(ic.get("cost_of_revenue", 0) / 1e6, 1) if ic.get("cost_of_revenue") else 0,
            gross_profit_m=round(ic.get("gross_profit", 0) / 1e6, 1) if ic.get("gross_profit") else 0,
            operating_income_m=round(ic.get("operating_income", 0) / 1e6, 1) if ic.get("operating_income") else 0,
            sga_m=round(ic.get("sga", 0) / 1e6, 1) if ic.get("sga") else 0,
            rd_m=round(ic.get("research_and_development", 0) / 1e6, 1) if ic.get("research_and_development") else 0,
            da_m=round(ic.get("depreciation_amortization", 0) / 1e6, 1) if ic.get("depreciation_amortization") else 0,
            interest_expense_m=round(ic.get("interest_expense", 0) / 1e6, 1) if ic.get("interest_expense") else 0,
            interest_income_m=round(ic.get("interest_income", 0) / 1e6, 1) if ic.get("interest_income") else 0,
            net_interest_m=round(net_interest / 1e6, 1) if net_interest else 0,
            pretax_income_m=round(ic.get("pretax_income", 0) / 1e6, 1) if ic.get("pretax_income") else 0,
            tax_expense_m=round(ic.get("tax_expense", 0) / 1e6, 1) if ic.get("tax_expense") else 0,
            net_income_m=round(ic.get("net_income", 0) / 1e6, 1) if ic.get("net_income") else 0,
            diluted_eps=diluted_eps,
            diluted_shares_m=diluted_shares_m,
            gross_margin_pct=pcts.get("gross_margin_pct", 0),
            cost_of_revenue_pct=pcts.get("cost_of_revenue_pct", 0),
            sga_pct=pcts.get("sga_pct", 0),
            rd_pct=pcts.get("rd_pct", 0),
            operating_margin_pct=pcts.get("operating_margin_pct", 0),
            tax_rate=data.get("tax_rate", 0),
            has_detailed_costs=bool(ic.get("sga")),
            has_per_share=bool(diluted_eps),
        )

        v(f"  Alpha Vantage financials: FY{sf.fiscal_year} rev=${sf.revenue_m:,.1f}M")
        return sf

    except Exception as e:
        v(f"  Alpha Vantage financials: error - {e}")
        return None


def _from_registry(ticker: str, data: dict) -> StructuredFinancials:
    """Build StructuredFinancials from company_registry cache."""
    py = data.get("prior_year", {})
    c = data.get("constants", {})

    revenue_m = py.get("revenue_m", 0)

    # Reconstruct from registry cost buckets
    # Registry stores costs as % of revenue, so multiply back
    cost_of_rev_pct = (
        py.get("food_pct", 0) + py.get("labor_pct", 0) +
        py.get("occupancy_pct", 0) + py.get("other_operating_pct", 0) +
        py.get("cogs_pct", 0) + py.get("cos_pct", 0)
    )
    cost_of_rev_m = revenue_m * cost_of_rev_pct / 100 if cost_of_rev_pct else 0
    gross_profit_m = revenue_m - cost_of_rev_m

    sga_m = py.get("cash_ga_m", 0) + py.get("stock_comp_m", 0)
    if not sga_m:
        sga_pct = py.get("sga_pct", 0) + py.get("ga_pct", 0)
        sga_m = revenue_m * sga_pct / 100 if sga_pct else 0

    da_m = py.get("da_m", 0)
    net_interest = c.get("net_interest_m", 0)
    tax_rate = c.get("tax_rate", 0.25)
    shares_m = c.get("shares_m", 100)

    operating_income_m = gross_profit_m - sga_m - da_m
    pretax_m = operating_income_m + net_interest
    net_income_m = pretax_m * (1 - tax_rate)
    eps = net_income_m / shares_m if shares_m else 0

    # Get actuals if available for EPS
    actuals = data.get("actuals", {})
    if actuals and actuals.get("eps"):
        eps = actuals["eps"]

    return StructuredFinancials(
        source="registry",
        ticker=ticker,
        fiscal_year=0,
        revenue_m=revenue_m,
        cost_of_revenue_m=round(cost_of_rev_m, 1),
        gross_profit_m=round(gross_profit_m, 1),
        operating_income_m=round(operating_income_m, 1),
        sga_m=round(sga_m, 1),
        da_m=da_m,
        net_interest_m=net_interest,
        pretax_income_m=round(pretax_m, 1),
        tax_expense_m=round(pretax_m * tax_rate, 1),
        net_income_m=round(net_income_m, 1),
        diluted_eps=round(eps, 2),
        diluted_shares_m=shares_m,
        gross_margin_pct=round(gross_profit_m / revenue_m * 100, 1) if revenue_m else 0,
        cost_of_revenue_pct=round(cost_of_rev_pct, 1),
        sga_pct=round(sga_m / revenue_m * 100, 1) if revenue_m else 0,
        operating_margin_pct=round(operating_income_m / revenue_m * 100, 1) if revenue_m else 0,
        tax_rate=tax_rate,
        has_detailed_costs=False,
        has_per_share=True,
    )
