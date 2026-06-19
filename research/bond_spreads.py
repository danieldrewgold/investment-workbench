"""
Bond spread calculations.

Given a clean bond price (% of par), coupon, maturity, optional call
schedule, plus a Treasury yield curve, compute:

    YTM    yield to maturity
    YTC    yield to first call (if callable, at the call price)
    YTW    yield to worst = min(YTM, YTC)
    spread the G-spread approximation: YTW minus interpolated UST yield
            at the matching horizon, in bps

V1 design choices:

  * G-spread (yield-to-worst minus a single interpolated par UST yield)
    is the practitioner shorthand for callable corporates. True OAS
    requires a short-rate tree with monthly rate scenarios — that's a
    separate quant project. G-spread captures the same direction signal
    for "is this bond widening?" comparisons within ~10 bps of OAS for
    most names.

  * Semi-annual coupon timing. Treats the next coupon as exactly half a
    year out. Real bond settlement uses actual day-count, but the error
    is well below 1bp for monitoring purposes and avoids a calendar
    dependency.

  * Newton's method on YTM with bisection fallback. Converges in <10
    iterations for well-formed inputs; falls back to bisection over
    [-50%, +200%] if Newton diverges.

Public API:

    bond_yields(price, coupon_pct, years_to_maturity,
                call_dates_years=None, call_prices=None)
        -> {ytm_pct, ytc_pct (or None), ytw_pct, ytw_horizon_years}

    g_spread_bps(ytw_pct, ytw_horizon_years, treasury_curve)
        -> spread in bps, or None if curve missing
"""

from __future__ import annotations


def _price_at_yield(
    y: float,
    coupon_pct: float,
    years_to_maturity: float,
    *,
    final_principal: float = 100.0,
    freq: int = 2,
    face: float = 100.0,
) -> float:
    """Compute clean price for a given yield. Cash flows: semi-annual
    coupon `coupon_pct/freq * face` for n periods, plus `final_principal`
    at the end (== 100 for held-to-maturity, == call_price for held-to-call).
    Time to next coupon assumed = 1/freq years (no accrued)."""
    n = max(int(round(years_to_maturity * freq)), 1)
    if n == 0:
        return final_principal
    cpn = (coupon_pct / 100.0) * face / freq
    yp = y / freq
    # Sum of geometric series for discount factors at each coupon date
    if abs(yp) < 1e-12:
        annuity = n
        df_n = 1.0
    else:
        df_n = (1.0 + yp) ** -n
        annuity = (1.0 - df_n) / yp
    return cpn * annuity + final_principal * df_n


def _solve_yield(
    price: float,
    coupon_pct: float,
    years: float,
    *,
    final_principal: float = 100.0,
    freq: int = 2,
    face: float = 100.0,
) -> float | None:
    """Solve for yield given price. Newton with bisection fallback."""
    if years <= 0 or price <= 0:
        return None

    # Sensible starting guess: current-yield + amortized capital gain
    cy = (coupon_pct / 100.0) * face / price
    cap_gain = (final_principal - price) / years / face
    y = cy + cap_gain
    if y < -0.20:
        y = 0.05
    if y > 1.0:
        y = 0.10

    # Newton's method
    for _ in range(60):
        p_y = _price_at_yield(
            y, coupon_pct, years,
            final_principal=final_principal, freq=freq, face=face,
        )
        diff = p_y - price
        if abs(diff) < 1e-8:
            return y
        # Numerical derivative (analytical is fragile near y=0)
        eps = 1e-6
        deriv = (
            _price_at_yield(y + eps, coupon_pct, years,
                            final_principal=final_principal, freq=freq, face=face)
            - _price_at_yield(y - eps, coupon_pct, years,
                              final_principal=final_principal, freq=freq, face=face)
        ) / (2 * eps)
        if deriv == 0:
            break
        y_new = y - diff / deriv
        if y_new < -0.5 or y_new > 5.0:
            break
        y = y_new

    # Bisection fallback over a wide range
    lo, hi = -0.49, 2.0
    p_lo = _price_at_yield(lo, coupon_pct, years,
                            final_principal=final_principal, freq=freq, face=face)
    p_hi = _price_at_yield(hi, coupon_pct, years,
                            final_principal=final_principal, freq=freq, face=face)
    # Price is monotonically decreasing in yield. Want price = `price`.
    if (p_lo - price) * (p_hi - price) > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        p_mid = _price_at_yield(mid, coupon_pct, years,
                                 final_principal=final_principal, freq=freq, face=face)
        if abs(p_mid - price) < 1e-8:
            return mid
        if (p_mid - price) * (p_lo - price) < 0:
            hi = mid
        else:
            lo = mid
            p_lo = p_mid
    return (lo + hi) / 2


def bond_yields(
    price: float,
    coupon_pct: float,
    years_to_maturity: float,
    *,
    call_dates_years: list[float] | None = None,
    call_prices: list[float] | None = None,
    freq: int = 2,
) -> dict:
    """
    Compute YTM, YTC at first call, and YTW.

    Args:
        price: clean price as % of par (100 = par)
        coupon_pct: annual coupon rate in percent (3.0 = 3%)
        years_to_maturity: time to maturity in years
        call_dates_years: list of call dates expressed as years-from-now.
            Empty/None for non-callable. Only the first (earliest) is used
            for YTW computation in v1.
        call_prices: list of call prices (% of par) corresponding to
            call_dates_years. Defaults to 100 (par) if not provided.

    Returns dict with:
        ytm_pct          (yield to maturity, % — e.g. 4.523)
        ytc_pct          (yield to first call, % — None if non-callable)
        ytw_pct          (yield to worst, %)
        ytw_horizon_years (the horizon corresponding to YTW: maturity if
                          YTM is the worst, first-call date if YTC wins)
    """
    out = {
        "ytm_pct": None,
        "ytc_pct": None,
        "ytw_pct": None,
        "ytw_horizon_years": None,
    }

    ytm = _solve_yield(price, coupon_pct, years_to_maturity, final_principal=100.0,
                       freq=freq)
    if ytm is None:
        return out
    out["ytm_pct"] = ytm * 100

    ytc = None
    ytc_horizon = None
    if call_dates_years:
        # Use the EARLIEST callable date for YTW (most conservative for the
        # holder when bond trades > call price; least restrictive otherwise)
        for i, t_call in enumerate(call_dates_years):
            if t_call is None or t_call <= 0 or t_call >= years_to_maturity:
                continue
            cp = (call_prices[i] if (call_prices and i < len(call_prices) and
                                      call_prices[i] is not None) else 100.0)
            y_c = _solve_yield(price, coupon_pct, t_call, final_principal=cp,
                                freq=freq)
            if y_c is None:
                continue
            if ytc is None or y_c < ytc:
                ytc = y_c
                ytc_horizon = t_call
    if ytc is not None:
        out["ytc_pct"] = ytc * 100
    # YTW
    if ytc is None:
        out["ytw_pct"] = ytm * 100
        out["ytw_horizon_years"] = years_to_maturity
    else:
        if ytm <= ytc:
            out["ytw_pct"] = ytm * 100
            out["ytw_horizon_years"] = years_to_maturity
        else:
            out["ytw_pct"] = ytc * 100
            out["ytw_horizon_years"] = ytc_horizon
    return out


def g_spread_bps(
    ytw_pct: float,
    ytw_horizon_years: float,
    treasury_curve,
) -> float | None:
    """G-spread = bond YTW minus interpolated treasury par yield at the
    matching horizon, in basis points. Returns None if treasury_curve is
    None or the interpolation fails.

    treasury_curve is a TreasuryCurve instance from
    `ingestion.loaders.treasury_curve_loader`. We only call its
    `.interpolate()` method, so any duck-typed object works for testing.
    """
    if treasury_curve is None or ytw_pct is None:
        return None
    ust_pct = treasury_curve.interpolate(ytw_horizon_years)
    if ust_pct is None:
        return None
    return (ytw_pct - ust_pct) * 100  # convert pp to bps


# --------------------------------------------------------------------------
# Sanity tests (run via `python -m research.bond_spreads`)
# --------------------------------------------------------------------------

def _self_test():
    """Quick sanity checks — not a full unit test suite, but enough to
    catch regressions in the yield solver."""
    # 1. Bond at par should have YTM == coupon
    r = bond_yields(100.0, 5.0, 10.0)
    assert abs(r["ytm_pct"] - 5.0) < 0.01, f"par bond YTM {r['ytm_pct']}, expected 5.0"

    # 2. Discount bond: 5% coupon, 10y, $90 → YTM > 5%
    r = bond_yields(90.0, 5.0, 10.0)
    assert r["ytm_pct"] > 5.0, f"discount bond YTM should exceed coupon, got {r['ytm_pct']}"
    # In this regime, YTM ≈ 6.38% (verified by hand calc)
    assert 6.0 < r["ytm_pct"] < 6.7, f"discount bond YTM out of range: {r['ytm_pct']}"

    # 3. Premium bond: 5% coupon, 10y, $110 → YTM < 5%
    r = bond_yields(110.0, 5.0, 10.0)
    assert r["ytm_pct"] < 5.0, f"premium bond YTM should be below coupon, got {r['ytm_pct']}"

    # 4. Callable bond — YTC at $100 in 2y vs YTM in 10y
    # Premium bond → YTC will be lower than YTM (worst case for holder
    # is being called early at par when bond was bought above par)
    r = bond_yields(105.0, 5.0, 10.0, call_dates_years=[2.0], call_prices=[100.0])
    assert r["ytc_pct"] is not None, "YTC should compute for callable"
    assert r["ytw_pct"] <= r["ytm_pct"] + 1e-6, (
        f"YTW {r['ytw_pct']} should be ≤ YTM {r['ytm_pct']}"
    )

    # 5. G-spread: stub a curve that returns 4% flat
    class _FlatCurve:
        def interpolate(self, y): return 4.0
    spread = g_spread_bps(5.5, 5.0, _FlatCurve())
    assert abs(spread - 150) < 0.001, f"g_spread expected 150, got {spread}"

    print("bond_spreads self-tests passed")


if __name__ == "__main__":
    _self_test()
