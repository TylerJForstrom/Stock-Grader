"""Fundamental metrics: valuation, profitability, growth, solvency, efficiency, quality, payout.

Every metric returns ``float`` or ``None``. ``None`` means "could not be computed" and is handled
by the engine as missing — never substituted with a zero, which would be an assertion that the
company scored badly rather than that we do not know.

Valuation multiples all pass ``positive_denominator=True``. This single flag is the difference
between a working value screen and a broken one: without it a company that lost money reports a
negative P/E, which sorts to the top of "cheapest first" and puts the most distressed names in the
portfolio.
"""

from __future__ import annotations

import pandas as pd

from ..registry import metric
from ..types import SectorClass, SecuritySnapshot
from .util import cagr, consistency, linear_trend, r_squared_loglinear, safe_div

# Multiples above these are arithmetically true but analytically meaningless; clamping stops a
# company that earned a rounding error from dominating the cross-section.
_MULTIPLE_CAP = 500.0


def _f(snapshot: SecuritySnapshot):
    return snapshot.fundamentals


MAX_BALANCE_AGE_DAYS = 400


def _aligned_annual_model_frame(
    snapshot: SecuritySnapshot,
    concepts: tuple[str, ...],
    *,
    periods: int,
    required_periods: dict[str, int] | None = None,
) -> pd.DataFrame | None:
    """Shared, consecutive, current fiscal rows for published accounting models."""
    fundamentals = _f(snapshot)
    if fundamentals is None:
        return None
    return fundamentals.aligned_annual(
        concepts,
        periods=periods,
        asof=snapshot.asof,
        max_age_days=MAX_BALANCE_AGE_DAYS,
        required_periods=required_periods,
    )


def _ttm(snapshot: SecuritySnapshot, concept: str) -> float | None:
    """Trailing-twelve-month value, age-bounded, falling back to the annual frame.

    The fallback matters: when the quarterly series is stale the correct figure is usually sitting
    in the *same object's* annual frame — Mastercard's annual net income is $14.97B against the
    $3.22B its 2014-era quarterly window produced. Refusing outright would delete 153 metric inputs
    across 60 of 82 default-universe companies for data that is available.

    Both paths carry the same age bound, so the worst remaining mismatch inside a ratio is an
    annual figure ending one quarter before a trailing-twelve-month one — a normal and small
    offset, not the twelve-year gap this replaced.
    """
    f = _f(snapshot)
    if f is None:
        return None
    value = f.ttm(concept, asof=snapshot.asof, max_age_days=MAX_BALANCE_AGE_DAYS)
    if value is not None:
        return value
    return f.latest(concept, annual=True, asof=snapshot.asof, max_age_days=MAX_BALANCE_AGE_DAYS)


def _latest(snapshot: SecuritySnapshot, concept: str) -> float | None:
    f = _f(snapshot)
    if f is None:
        return None
    return f.latest(concept, asof=snapshot.asof, max_age_days=MAX_BALANCE_AGE_DAYS)


def _ttm_with_period_end(
    snapshot: SecuritySnapshot,
    concept: str,
) -> tuple[float | None, pd.Timestamp | None]:
    """Age-bounded trailing flow and the dated period it represents.

    Return metrics need a denominator measured over the same interval as their numerator.  The
    scalar-only ``_ttm`` helper cannot establish that alignment, so this companion mirrors its
    quarterly-then-annual fallback and retains the selected period end.
    """
    f = _f(snapshot)
    if f is None:
        return (None, None)

    value = f.ttm(concept, asof=snapshot.asof, max_age_days=MAX_BALANCE_AGE_DAYS)
    if value is not None:
        series = f._dated_series(f.quarterly, concept, asof=snapshot.asof)
        if series is not None:
            return (float(value), pd.Timestamp(series.index[-1]))
        return (float(value), None)

    value = f.latest(
        concept,
        annual=True,
        asof=snapshot.asof,
        max_age_days=MAX_BALANCE_AGE_DAYS,
    )
    if value is None:
        return (None, None)
    series = f._dated_series(f.annual, concept, asof=snapshot.asof)
    if series is not None:
        return (float(value), pd.Timestamp(series.index[-1]))
    return (float(value), None)


def _average_balance_for_period(
    snapshot: SecuritySnapshot,
    concept: str,
    period_end: pd.Timestamp | None,
) -> float | None:
    """Beginning/ending average balance aligned to a trailing flow.

    A return earned throughout a year should not be divided only by the balance left on the final
    day.  We average observations approximately one year apart when the ending balance is dated
    within 45 days of the numerator's period end.  If aligned history is unavailable, the latest
    positive balance is retained as a conservative coverage-preserving fallback.  If an aligned
    pair exists but either endpoint is non-positive, the ratio is undefined rather than silently
    reverting to the more flattering ending balance.
    """
    f = _f(snapshot)
    if f is None:
        return None

    series_by_frame: list[pd.Series] = []
    for frame in (f.quarterly, f.annual):
        if concept not in frame.columns:
            continue
        raw = frame[concept].dropna()
        if raw.empty:
            continue
        try:
            series = pd.Series(
                raw.to_numpy(dtype="float64"),
                index=pd.to_datetime(raw.index),
                dtype="float64",
            )
        except (TypeError, ValueError):
            continue
        series = series.groupby(level=0).last().sort_index()
        series_by_frame.append(series)

    if period_end is not None:
        end = pd.Timestamp(period_end)
        for series in series_by_frame:
            eligible = series.loc[series.index <= end]
            if eligible.empty:
                continue
            ending_date = pd.Timestamp(eligible.index[-1])
            if (end - ending_date).days > 45:
                continue
            beginning_candidates = eligible.iloc[:-1]
            if beginning_candidates.empty:
                continue
            ages = pd.Series(
                [(ending_date - pd.Timestamp(idx)).days for idx in beginning_candidates.index],
                index=beginning_candidates.index,
                dtype="float64",
            )
            aligned = ages[ages.between(300, 430)]
            if aligned.empty:
                continue
            beginning_date = (aligned - 365.25).abs().idxmin()
            beginning = float(beginning_candidates.loc[beginning_date])
            ending = float(eligible.iloc[-1])
            if beginning <= 0.0 or ending <= 0.0:
                return None
            return (beginning + ending) / 2.0

    # Preserve coverage when the company only exposes an ending balance, but do not search beyond
    # the same age bound used everywhere else in the fundamental metric layer.
    latest = _latest(snapshot, concept)
    if latest is None:
        latest = f.latest(
            concept,
            annual=True,
            asof=snapshot.asof,
            max_age_days=MAX_BALANCE_AGE_DAYS,
        )
    return float(latest) if latest is not None and latest > 0.0 else None


def _altman_model_for(snapshot: SecuritySnapshot) -> str | None:
    """The single Altman variant supported by this security's actual business model.

    The original Z model was estimated on publicly traded manufacturers (SIC 2000--3999).  Z''
    removes the sales/asset term for other operating businesses.  Neither variant is defensible
    for financials, REITs, holding companies, or regulated utilities.  When SIC is absent we
    decline both instead of giving the same company two correlated distress votes.
    """
    if snapshot.sector in {
        SectorClass.BANK,
        SectorClass.INSURANCE,
        SectorClass.REIT,
        SectorClass.HOLDING,
        SectorClass.UTILITY,
    }:
        return None
    if snapshot.sic is None:
        return None
    try:
        sic = int(str(snapshot.sic).strip())
    except (TypeError, ValueError):
        return None
    return "z" if 2000 <= sic <= 3999 else "z_prime"


def _enterprise_value(snapshot: SecuritySnapshot) -> float | None:
    """Market cap plus debt less cash. ``None`` when debt is unknown.

    ``debt or 0.0`` was the bug here: it silently converted "we could not read this company's debt"
    into "this company has no debt", which is a one-directional optimistic error across every
    EV-based multiple — an unknown-debt company looked cheaper than a debt-free one is entitled to.
    Cash keeps the zero default, because that error runs the other way and makes a company look
    more expensive, and because a missing cash line is far rarer.
    """
    cap = snapshot.market_cap
    if cap is None:
        return None
    debt = _latest(snapshot, "total_debt")
    if debt is None:
        return None
    cash = _latest(snapshot, "cash") or 0.0
    ev = cap + debt - cash
    return ev if ev > 0 else None


# ---------------------------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------------------------


@metric("pe_trailing", group="earnings_multiple", pillar="valuation", direction=-1, unit="x",
        description="Price / trailing twelve-month earnings", winsor=(0.0, _MULTIPLE_CAP))
def pe_trailing(s: SecuritySnapshot) -> float | None:
    """Undefined for loss-makers rather than negative — a negative P/E is not a cheap one."""
    return safe_div(s.market_cap, _ttm(s, "net_income"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_sales", group="sales_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_sales(s: SecuritySnapshot) -> float | None:
    """Price / trailing revenue. Meaningful even when earnings are negative."""
    return safe_div(s.market_cap, _ttm(s, "revenue"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_book", group="book_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_book(s: SecuritySnapshot) -> float | None:
    """Price / shareholders' equity. Undefined when book value is negative."""
    return safe_div(s.market_cap, _latest(s, "equity"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_tangible_book", group="book_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_tangible_book(s: SecuritySnapshot) -> float | None:
    """Price / book value excluding goodwill and intangibles."""
    return safe_div(s.market_cap, _latest(s, "tangible_book"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_fcf", group="fcf_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_fcf(s: SecuritySnapshot) -> float | None:
    """Price / free cash flow."""
    return safe_div(s.market_cap, _ttm(s, "fcf"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_ocf", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_ocf(s: SecuritySnapshot) -> float | None:
    """Price / cash from operations — harder to manipulate than earnings."""
    return safe_div(s.market_cap, _ttm(s, "cfo"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_ebitda", group="ebit_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_ebitda(s: SecuritySnapshot) -> float | None:
    """Enterprise value / EBITDA — capital-structure neutral, so it compares across leverage."""
    return safe_div(_enterprise_value(s), _ttm(s, "ebitda"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_ebit", group="ebit_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_ebit(s: SecuritySnapshot) -> float | None:
    """Enterprise value / EBIT. Charges companies for depreciation, unlike EV/EBITDA."""
    return safe_div(_enterprise_value(s), _ttm(s, "ebit"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_sales", group="sales_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_sales(s: SecuritySnapshot) -> float | None:
    """Enterprise value / revenue."""
    return safe_div(_enterprise_value(s), _ttm(s, "revenue"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_fcf", group="fcf_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_fcf(s: SecuritySnapshot) -> float | None:
    """Enterprise value / free cash flow."""
    return safe_div(_enterprise_value(s), _ttm(s, "fcf"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("earnings_yield", group="earnings_multiple", pillar="valuation", direction=1, unit="ratio")
def earnings_yield(s: SecuritySnapshot) -> float | None:
    """Earnings / price. The reciprocal of P/E, but defined for loss-makers (it goes negative)."""
    return safe_div(_ttm(s, "net_income"), s.market_cap, positive_denominator=True)


@metric("fcf_yield", group="fcf_multiple", pillar="valuation", direction=1, unit="ratio")
def fcf_yield(s: SecuritySnapshot) -> float | None:
    """Free cash flow / market cap."""
    return safe_div(_ttm(s, "fcf"), s.market_cap, positive_denominator=True)


# NOTE: `acquirers_multiple` was removed rather than kept as an alias. Carlisle's Acquirer's
# Multiple is enterprise value over operating earnings, and `_derive` sets ``ebit =
# operating_income`` whenever the latter exists — so it computed a bit-identical value to
# `ev_to_ebit` and handed one valuation signal two votes, 11.8% of the valuation pillar under the
# shipped equal weighting. The sector matrix still references the name harmlessly.


@metric("ev_to_gross_profit", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_gross_profit(s: SecuritySnapshot) -> float | None:
    """EV / gross profit — the cleanest top-line profitability measure to pay for."""
    return safe_div(_enterprise_value(s), _ttm(s, "gross_profit"),
                    positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("peg_ratio", pillar="valuation", direction=-1, unit="x", winsor=(0.0, 20.0))
def peg_ratio(s: SecuritySnapshot) -> float | None:
    """P/E divided by earnings growth. Undefined without positive earnings *and* positive growth."""
    pe = pe_trailing.fn(s)
    f = _f(s)
    if pe is None or f is None:
        return None
    history = f.history("net_income", 4, annual=True)
    if history is None:
        return None
    growth = cagr(history.iloc[0], history.iloc[-1], len(history) - 1)
    if growth is None or growth <= 0:
        return None
    return safe_div(pe, growth * 100.0, positive_denominator=True, cap=20.0)


@metric("graham_number", pillar="valuation", direction=1, unit="ratio")
def graham_number_ratio(s: SecuritySnapshot) -> float | None:
    """Graham number / price — above 1 means the stock trades below Graham's fair value.

    ``sqrt(22.5 * EPS * book value per share)``, expressed as a ratio to price so it is comparable
    across securities rather than being a dollar figure.
    """
    f = _f(s)
    price = s.valuation_price
    if f is None or price is None or s.shares_outstanding in (None, 0):
        return None
    eps = _ttm(s, "net_income")
    equity = _latest(s, "equity")
    if eps is None or equity is None or eps <= 0 or equity <= 0:
        return None
    eps_ps = eps / s.shares_outstanding
    bvps = equity / s.shares_outstanding
    fair = (22.5 * eps_ps * bvps) ** 0.5
    return safe_div(fair, price, positive_denominator=True)


# ---------------------------------------------------------------------------------------------
# Profitability
# ---------------------------------------------------------------------------------------------


@metric("gross_margin", pillar="profitability", direction=1, unit="ratio")
def gross_margin(s: SecuritySnapshot) -> float | None:
    """Gross profit / revenue — the cleanest signal of pricing power."""
    return safe_div(_ttm(s, "gross_profit"), _ttm(s, "revenue"), positive_denominator=True)


@metric("operating_margin", pillar="profitability", direction=1, unit="ratio")
def operating_margin(s: SecuritySnapshot) -> float | None:
    """Operating income / revenue."""
    return safe_div(_ttm(s, "operating_income"), _ttm(s, "revenue"), positive_denominator=True)


@metric("net_margin", pillar="profitability", direction=1, unit="ratio")
def net_margin(s: SecuritySnapshot) -> float | None:
    """Net income / revenue."""
    return safe_div(_ttm(s, "net_income"), _ttm(s, "revenue"), positive_denominator=True)


@metric("ebitda_margin", pillar="profitability", direction=1, unit="ratio")
def ebitda_margin(s: SecuritySnapshot) -> float | None:
    """EBITDA / revenue."""
    return safe_div(_ttm(s, "ebitda"), _ttm(s, "revenue"), positive_denominator=True)


@metric("free_cash_flow_margin", pillar="profitability", direction=1, unit="ratio")
def fcf_margin(s: SecuritySnapshot) -> float | None:
    """Free cash flow / revenue."""
    return safe_div(_ttm(s, "fcf"), _ttm(s, "revenue"), positive_denominator=True)


@metric("roe", pillar="profitability", direction=1, unit="ratio")
def roe(s: SecuritySnapshot) -> float | None:
    """Return on equity. Note a highly levered company can post a great ROE on a thin equity base,
    which is why the solvency pillar exists alongside this one."""
    income, period_end = _ttm_with_period_end(s, "net_income")
    equity = _average_balance_for_period(s, "equity", period_end)
    return safe_div(income, equity, positive_denominator=True)


@metric("roa", pillar="profitability", direction=1, unit="ratio")
def roa(s: SecuritySnapshot) -> float | None:
    """Return on assets."""
    income, period_end = _ttm_with_period_end(s, "net_income")
    assets = _average_balance_for_period(s, "assets", period_end)
    return safe_div(income, assets, positive_denominator=True)


@metric("roic", pillar="profitability", direction=1, unit="ratio")
def roic(s: SecuritySnapshot) -> float | None:
    """After-tax operating profit / invested capital — the best single measure of business quality."""
    ebit, period_end = _ttm_with_period_end(s, "ebit")
    tax = _ttm(s, "income_tax")
    pretax = _ttm(s, "pretax_income")
    if ebit is None:
        return None
    rate = safe_div(tax, pretax, positive_denominator=True)
    rate = 0.21 if rate is None or not (0.0 <= rate <= 0.6) else rate
    invested_capital = _average_balance_for_period(s, "invested_capital", period_end)
    return safe_div(ebit * (1 - rate), invested_capital, positive_denominator=True)


@metric("croic", pillar="profitability", direction=1, unit="ratio")
def croic(s: SecuritySnapshot) -> float | None:
    """Cash return on invested capital — ROIC computed from free cash flow instead of earnings."""
    return safe_div(_ttm(s, "fcf"), _latest(s, "invested_capital"), positive_denominator=True)


@metric("gross_profit_to_assets", pillar="profitability", direction=1, unit="ratio")
def gross_profit_to_assets(s: SecuritySnapshot) -> float | None:
    """Novy-Marx gross profitability: gross profit / total assets.

    Robustly predicts returns better than earnings-based profitability, because gross profit sits
    higher on the income statement and so is less contaminated by accounting discretion.
    """
    return safe_div(_ttm(s, "gross_profit"), _latest(s, "assets"), positive_denominator=True)


@metric("margin_trend", pillar="profitability", direction=1, unit="t-stat")
def margin_trend(s: SecuritySnapshot) -> float | None:
    """t-statistic of the trend in annual operating margin — is the business getting better?"""
    f = _f(s)
    if f is None:
        return None
    revenue = f.history("revenue", 5, annual=True)
    income = f.history("operating_income", 5, annual=True)
    if revenue is None or income is None or len(revenue) != len(income):
        return None
    margins = (income / revenue).dropna()
    result = linear_trend(margins)
    return result[1] if result else None


# ---------------------------------------------------------------------------------------------
# Growth
# ---------------------------------------------------------------------------------------------


def _cagr_metric(s: SecuritySnapshot, concept: str, years: int) -> float | None:
    f = _f(s)
    if f is None:
        return None
    history = f.history(concept, years + 1, annual=True)
    if history is None:
        return None
    return cagr(history.iloc[0], history.iloc[-1], len(history) - 1)


@metric("revenue_cagr_3y", pillar="growth", direction=1, unit="ratio", winsor=(-1.0, 3.0))
def revenue_cagr_3y(s: SecuritySnapshot) -> float | None:
    """Three-year revenue CAGR."""
    return _cagr_metric(s, "revenue", 3)


@metric("revenue_cagr_5y", pillar="growth", direction=1, unit="ratio", winsor=(-1.0, 3.0))
def revenue_cagr_5y(s: SecuritySnapshot) -> float | None:
    """Five-year revenue CAGR."""
    return _cagr_metric(s, "revenue", 5)


@metric("earnings_cagr_3y", pillar="growth", direction=1, unit="ratio", winsor=(-1.0, 3.0))
def earnings_cagr_3y(s: SecuritySnapshot) -> float | None:
    """Three-year net income CAGR. ``None`` when the base year was a loss."""
    return _cagr_metric(s, "net_income", 3)


@metric("earnings_cagr_5y", pillar="growth", direction=1, unit="ratio", winsor=(-1.0, 3.0))
def earnings_cagr_5y(s: SecuritySnapshot) -> float | None:
    """Five-year net income CAGR."""
    return _cagr_metric(s, "net_income", 5)


@metric("fcf_cagr_3y", pillar="growth", direction=1, unit="ratio", winsor=(-1.0, 3.0))
def fcf_cagr_3y(s: SecuritySnapshot) -> float | None:
    """Three-year free cash flow CAGR."""
    return _cagr_metric(s, "fcf", 3)


@metric("book_value_cagr_5y", pillar="growth", direction=1, unit="ratio", winsor=(-1.0, 3.0))
def book_value_cagr_5y(s: SecuritySnapshot) -> float | None:
    """Five-year split-adjusted book value *per share* growth.

    Total equity growth is not per-share growth: an acquisition funded by doubling both equity and
    the share count creates no book value for an existing share. Equity and share counts are joined
    on the same fiscal dates, and the share history is put on the current split basis using assets
    to distinguish a split from genuine issuance. Missing or misaligned share history returns
    ``None`` rather than quietly reverting to total book value.
    """
    f = _f(s)
    if f is None or f.annual.empty or "equity" not in f.annual.columns:
        return None

    from ..data.sec import restate_for_splits

    equity = f.annual["equity"].dropna()
    if equity.empty:
        return None
    scale_reference = f.annual["assets"].dropna() if "assets" in f.annual.columns else None
    for share_concept in ("shares_diluted", "shares_basic"):
        if share_concept not in f.annual.columns:
            continue
        shares = f.annual[share_concept].dropna()
        if shares.empty:
            continue
        adjusted_shares = restate_for_splits(shares, scale_reference)
        aligned = pd.concat(
            [equity.rename("equity"), adjusted_shares.rename("shares")],
            axis=1,
            join="inner",
        ).dropna()
        aligned = aligned[(aligned["equity"] > 0.0) & (aligned["shares"] > 0.0)]
        if len(aligned) < 6:
            continue
        window = aligned.iloc[-6:]
        try:
            dates = pd.to_datetime(window.index)
        except (TypeError, ValueError):
            continue
        gaps = pd.Series(dates[1:] - dates[:-1]).dt.days
        elapsed_years = (dates[-1] - dates[0]).days / 365.25
        if not gaps.between(270, 460).all() or not 4.5 <= elapsed_years <= 5.5:
            continue
        bvps = window["equity"] / window["shares"]
        return cagr(bvps.iloc[0], bvps.iloc[-1], elapsed_years)
    return None


@metric("revenue_growth_consistency", pillar="growth", direction=1, unit="r2")
def revenue_growth_consistency(s: SecuritySnapshot) -> float | None:
    """R-squared of a log-linear fit to revenue.

    Separates two companies with identical CAGR: one that compounded steadily and one that
    collapsed and rebounded. The first is a much better business.
    """
    f = _f(s)
    if f is None:
        return None
    history = f.history("revenue", 6, annual=True)
    return r_squared_loglinear(history) if history is not None else None


@metric("earnings_growth_consistency", pillar="growth", direction=1, unit="fraction")
def earnings_growth_consistency(s: SecuritySnapshot) -> float | None:
    """Fraction of years in which earnings increased."""
    f = _f(s)
    if f is None:
        return None
    history = f.history("net_income", 6, annual=True)
    return consistency(list(history)) if history is not None else None


@metric("revenue_growth_acceleration", pillar="growth", direction=1, unit="ratio", winsor=(-2.0, 2.0))
def revenue_growth_acceleration(s: SecuritySnapshot) -> float | None:
    """Recent growth minus older growth — the second derivative of revenue."""
    recent = _cagr_metric(s, "revenue", 2)
    longer = _cagr_metric(s, "revenue", 5)
    if recent is None or longer is None:
        return None
    return recent - longer


# ---------------------------------------------------------------------------------------------
# Financial health
# ---------------------------------------------------------------------------------------------


@metric("current_ratio", pillar="health", direction=1, unit="x", winsor=(0.0, 20.0))
def current_ratio(s: SecuritySnapshot) -> float | None:
    """Current assets / current liabilities. Not defined for banks — see the sector matrix."""
    return safe_div(_latest(s, "current_assets"), _latest(s, "current_liabilities"),
                    positive_denominator=True, cap=20.0)


@metric("quick_ratio", pillar="health", direction=1, unit="x", winsor=(0.0, 20.0))
def quick_ratio(s: SecuritySnapshot) -> float | None:
    """(Current assets - inventory) / current liabilities."""
    current = _latest(s, "current_assets")
    if current is None:
        return None
    inventory = _latest(s, "inventory") or 0.0
    return safe_div(current - inventory, _latest(s, "current_liabilities"),
                    positive_denominator=True, cap=20.0)


@metric("cash_ratio", pillar="health", direction=1, unit="x", winsor=(0.0, 20.0))
def cash_ratio(s: SecuritySnapshot) -> float | None:
    """Cash / current liabilities."""
    return safe_div(_latest(s, "cash"), _latest(s, "current_liabilities"),
                    positive_denominator=True, cap=20.0)


@metric("debt_to_equity", pillar="health", direction=-1, unit="x", winsor=(0.0, 20.0))
def debt_to_equity(s: SecuritySnapshot) -> float | None:
    """Total debt / equity."""
    return safe_div(_latest(s, "total_debt"), _latest(s, "equity"),
                    positive_denominator=True, cap=20.0)


@metric("net_debt_to_ebitda", pillar="health", direction=-1, unit="x", winsor=(-10.0, 30.0))
def net_debt_to_ebitda(s: SecuritySnapshot) -> float | None:
    """Net debt / EBITDA — the leverage measure lenders actually use.

    Negative values (net cash) are genuinely good and are preserved rather than clamped to zero.
    """
    return safe_div(_latest(s, "net_debt"), _ttm(s, "ebitda"), positive_denominator=True, cap=30.0)


@metric("interest_coverage", pillar="health", direction=1, unit="x", winsor=(-10.0, 100.0))
def interest_coverage(s: SecuritySnapshot) -> float | None:
    """EBIT / interest expense. ``None`` when there is no interest expense — not infinity."""
    interest = _ttm(s, "interest_expense")
    if interest is None or abs(interest) < 1e-9:
        return None
    return safe_div(_ttm(s, "ebit"), abs(interest), cap=100.0)


@metric("debt_to_assets", pillar="health", direction=-1, unit="ratio")
def debt_to_assets(s: SecuritySnapshot) -> float | None:
    """Total debt / total assets."""
    return safe_div(_latest(s, "total_debt"), _latest(s, "assets"), positive_denominator=True)


@metric("fcf_to_debt", pillar="health", direction=1, unit="ratio", winsor=(-5.0, 10.0))
def fcf_to_debt(s: SecuritySnapshot) -> float | None:
    """Free cash flow / total debt — how fast the company could repay from operations."""
    debt = _latest(s, "total_debt")
    if debt is None or debt <= 0:
        return None
    return safe_div(_ttm(s, "fcf"), debt, cap=10.0)


@metric("altman_z", group="altman", pillar="health", direction=1, unit="score", winsor=(-10.0, 20.0))
def altman_z(s: SecuritySnapshot) -> float | None:
    """Altman Z-score for public manufacturers.

    ``1.2*WC/TA + 1.4*RE/TA + 3.3*EBIT/TA + 0.6*MVE/TL + 1.0*Sales/TA``. Below 1.81 is the distress
    zone, above 2.99 the safe zone. Deliberately unavailable for financials, REITs and holding
    companies: the model was never estimated on them and its working-capital and sales-to-assets
    terms are not meaningful there. The sector matrix enforces that.
    """
    if _altman_model_for(s) != "z":
        return None
    frame = _aligned_annual_model_frame(
        s,
        (
            "assets",
            "working_capital",
            "retained_earnings",
            "ebit",
            "liabilities",
            "revenue",
        ),
        periods=1,
    )
    if frame is None:
        return None
    current = frame.iloc[-1]
    assets = float(current["assets"])
    working_capital = float(current["working_capital"])
    retained = float(current["retained_earnings"])
    ebit = float(current["ebit"])
    liabilities = float(current["liabilities"])
    sales = float(current["revenue"])
    cap = s.market_cap
    if assets <= 0 or liabilities <= 0 or cap is None:
        return None
    return float(
        1.2 * (working_capital / assets)
        + 1.4 * (retained / assets)
        + 3.3 * (ebit / assets)
        + 0.6 * (cap / liabilities)
        + 1.0 * (sales / assets)
    )


@metric("piotroski_f_score", pillar="health", direction=1, unit="score")
def piotroski_f_score(s: SecuritySnapshot) -> tuple[float, dict] | None:
    """Piotroski F-score: nine binary financial-strength tests, 0-9.

    Profitability (4): positive ROA, positive CFO, rising ROA, CFO exceeding net income (accrual
    quality). Leverage/liquidity (3): falling long-term debt ratio, rising current ratio, no share
    issuance. Efficiency (2): rising gross margin, rising asset turnover.
    """
    frame = _aligned_annual_model_frame(
        s,
        (
            "assets",
            "net_income",
            "cfo",
            "long_term_debt",
            "current_assets",
            "current_liabilities",
            "shares_diluted",
            "gross_profit",
            "revenue",
        ),
        periods=3,
        required_periods={
            "assets": 3,
            "net_income": 2,
            "cfo": 1,
            "long_term_debt": 2,
            "current_assets": 2,
            "current_liabilities": 2,
            "shares_diluted": 2,
            "gross_profit": 2,
            "revenue": 2,
        },
    )
    if frame is None:
        return None
    opening = frame.iloc[0]
    prior = frame.iloc[1]
    current = frame.iloc[2]

    opening_assets = float(opening["assets"])
    prev_assets, curr_assets = float(prior["assets"]), float(current["assets"])
    prev_income, curr_income = float(prior["net_income"]), float(current["net_income"])
    curr_cfo = float(current["cfo"])
    if opening_assets <= 0 or prev_assets <= 0 or curr_assets <= 0:
        return None

    # Piotroski defines ROA, CFO, and asset turnover over beginning-of-year assets. Leverage uses
    # average assets for each fiscal year. Computing both year-over-year signals therefore needs
    # three asset observations even though the income-statement comparison spans two years.
    roa_curr = safe_div(curr_income, prev_assets, positive_denominator=True)
    roa_prev = safe_div(prev_income, opening_assets, positive_denominator=True)
    cfo_roa = safe_div(curr_cfo, prev_assets, positive_denominator=True)
    avg_assets_curr = (prev_assets + curr_assets) / 2.0
    avg_assets_prev = (opening_assets + prev_assets) / 2.0
    if any(value is None for value in (roa_curr, roa_prev, cfo_roa)):
        return None
    assert roa_curr is not None and roa_prev is not None and cfo_roa is not None

    prev_debt = float(prior["long_term_debt"])
    curr_debt = float(current["long_term_debt"])
    debt_curr = safe_div(curr_debt, avg_assets_curr, positive_denominator=True)
    debt_prev = safe_div(prev_debt, avg_assets_prev, positive_denominator=True)
    prev_ca, curr_ca = float(prior["current_assets"]), float(current["current_assets"])
    prev_cl = float(prior["current_liabilities"])
    curr_cl = float(current["current_liabilities"])
    cr_curr = safe_div(curr_ca, curr_cl, positive_denominator=True)
    cr_prev = safe_div(prev_ca, prev_cl, positive_denominator=True)
    prev_shares = float(prior["shares_diluted"])
    curr_shares = float(current["shares_diluted"])
    prev_gp, curr_gp = float(prior["gross_profit"]), float(current["gross_profit"])
    prev_rev, curr_rev = float(prior["revenue"]), float(current["revenue"])
    gm_curr = safe_div(curr_gp, curr_rev, positive_denominator=True)
    gm_prev = safe_div(prev_gp, prev_rev, positive_denominator=True)
    at_curr = safe_div(curr_rev, prev_assets, positive_denominator=True)
    at_prev = safe_div(prev_rev, opening_assets, positive_denominator=True)
    ratios = (debt_curr, debt_prev, cr_curr, cr_prev, gm_curr, gm_prev, at_curr, at_prev)
    if any(value is None for value in ratios):
        return None
    assert debt_curr is not None and debt_prev is not None
    assert cr_curr is not None and cr_prev is not None
    assert gm_curr is not None and gm_prev is not None
    assert at_curr is not None and at_prev is not None

    components = {
        "positive_roa": int(roa_curr > 0),
        "positive_cfo": int(curr_cfo > 0),
        "rising_roa": int(roa_curr > roa_prev),
        "cfo_exceeds_net_income": int(cfo_roa > roa_curr),
        "falling_leverage": int(debt_curr < debt_prev),
        "rising_current_ratio": int(cr_curr > cr_prev),
        "no_share_issuance": int(curr_shares <= prev_shares),
        "rising_gross_margin": int(gm_curr > gm_prev),
        "rising_asset_turnover": int(at_curr > at_prev),
    }
    score = int(sum(components.values()))
    raw_inputs: dict[str, object] = {
        **components,
        "n_components": 9,
        "opening_fiscal_period": frame.index[0].date().isoformat(),
        "prior_fiscal_period": frame.index[1].date().isoformat(),
        "current_fiscal_period": frame.index[2].date().isoformat(),
    }
    return (float(score), raw_inputs)


# ---------------------------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------------------------


@metric("asset_turnover", pillar="efficiency", direction=1, unit="x")
def asset_turnover(s: SecuritySnapshot) -> float | None:
    """Revenue / total assets."""
    revenue, period_end = _ttm_with_period_end(s, "revenue")
    assets = _average_balance_for_period(s, "assets", period_end)
    return safe_div(revenue, assets, positive_denominator=True)


@metric("inventory_turnover", pillar="efficiency", direction=1, unit="x", winsor=(0.0, 100.0))
def inventory_turnover(s: SecuritySnapshot) -> float | None:
    """COGS / inventory."""
    return safe_div(_ttm(s, "cogs"), _latest(s, "inventory"), positive_denominator=True, cap=100.0)


@metric("days_sales_outstanding", pillar="efficiency", direction=-1, unit="days", winsor=(0.0, 365.0))
def days_sales_outstanding(s: SecuritySnapshot) -> float | None:
    """Receivables / revenue * 365 — how long customers take to pay."""
    ratio = safe_div(_latest(s, "receivables"), _ttm(s, "revenue"), positive_denominator=True)
    return ratio * 365.0 if ratio is not None else None


@metric("days_inventory_outstanding", pillar="efficiency", direction=-1, unit="days", winsor=(0.0, 730.0))
def days_inventory_outstanding(s: SecuritySnapshot) -> float | None:
    """Inventory / COGS * 365."""
    ratio = safe_div(_latest(s, "inventory"), _ttm(s, "cogs"), positive_denominator=True)
    return ratio * 365.0 if ratio is not None else None


@metric("capex_intensity", pillar="efficiency", direction=-1, unit="ratio")
def capex_intensity(s: SecuritySnapshot) -> float | None:
    """Capex / revenue — how capital-hungry the business is."""
    capex = _ttm(s, "capex")
    if capex is None:
        return None
    return safe_div(abs(capex), _ttm(s, "revenue"), positive_denominator=True)


@metric("rnd_intensity", pillar="efficiency", direction=1, unit="ratio")
def rnd_intensity(s: SecuritySnapshot) -> float | None:
    """R&D / revenue. Higher is treated as better — reinvestment in future competitiveness."""
    return safe_div(_ttm(s, "rnd_expense"), _ttm(s, "revenue"), positive_denominator=True)


# ---------------------------------------------------------------------------------------------
# Earnings quality
# ---------------------------------------------------------------------------------------------


@metric("accruals_ratio", pillar="quality", direction=-1, unit="ratio", winsor=(-2.0, 2.0))
def accruals_ratio(s: SecuritySnapshot) -> float | None:
    """(Net income - CFO) / assets — Sloan's accrual anomaly. **Profitable companies only.**

    Earnings far above cash flow predict future disappointment, because the gap is accounting
    estimates rather than money.

    The positive-earnings gate is not fussiness — it was measured. Against 60 companies whose
    auditors issued going-concern opinions, this metric scored an AUC of **0.29**: worse than a coin
    flip, actively *rewarding* the distressed companies. The cause is that a large net loss makes
    ``NI - CFO`` strongly negative, which a lower-is-better metric reads as conservative accounting.
    Biora Therapeutics posted a $122M loss against $46M of cash burn and scored as having
    excellent earnings quality.

    Sloan estimated the anomaly on profitable firms, and for a loss-maker the ratio mostly measures
    the size of the writeoffs rather than the quality of the profits. So it is undefined here, which
    lets the solvency metrics speak instead.
    """
    income = _ttm(s, "net_income")
    cfo = _ttm(s, "cfo")
    if income is None or cfo is None or income <= 0:
        return None
    return safe_div(income - cfo, _latest(s, "assets"), positive_denominator=True)


@metric("cash_conversion", pillar="quality", direction=1, unit="ratio", winsor=(-5.0, 5.0))
def cash_conversion(s: SecuritySnapshot) -> float | None:
    """CFO / net income. Below 1 for long stretches means earnings are not turning into cash."""
    return safe_div(_ttm(s, "cfo"), _ttm(s, "net_income"), positive_denominator=True, cap=5.0)


@metric("fcf_to_net_income", pillar="quality", direction=1, unit="ratio", winsor=(-5.0, 5.0))
def fcf_to_net_income(s: SecuritySnapshot) -> float | None:
    """Free cash flow / net income."""
    return safe_div(_ttm(s, "fcf"), _ttm(s, "net_income"), positive_denominator=True, cap=5.0)


@metric("goodwill_to_assets", pillar="quality", direction=-1, unit="ratio")
def goodwill_to_assets(s: SecuritySnapshot) -> float | None:
    """Goodwill / assets — acquisition-heavy balance sheets carry write-down risk."""
    goodwill = _latest(s, "goodwill")
    if goodwill is None:
        return None
    return safe_div(goodwill, _latest(s, "assets"), positive_denominator=True)


@metric("share_count_change", pillar="quality", direction=-1, unit="ratio", winsor=(-0.5, 0.5))
def share_count_change(s: SecuritySnapshot) -> float | None:
    """Annualised change in diluted share count. Dilution is a direct transfer away from holders."""
    f = _f(s)
    if f is None:
        return None
    history = f.history("shares_diluted", 4, annual=True)
    if history is None or history.iloc[0] <= 0:
        return None
    years = len(history) - 1
    growth = cagr(history.iloc[0], history.iloc[-1], years)
    return growth


# ---------------------------------------------------------------------------------------------
# Shareholder return
# ---------------------------------------------------------------------------------------------


@metric("dividend_yield", pillar="shareholder", direction=1, unit="ratio", winsor=(0.0, 0.3))
def dividend_yield(s: SecuritySnapshot) -> float | None:
    """Dividends paid / market cap.

    Falls back to the foundry's reconstructed dividends-per-share (already on
    the current split basis) when the XBRL cash-flow tag is absent — small
    caps often skip the tag while the per-share history still exists.
    """
    dividends = _ttm(s, "dividends_paid")
    if dividends is not None:
        return safe_div(abs(dividends), s.market_cap, positive_denominator=True)
    foundry_dps = s.meta.get("foundry_dps_ttm") if s.meta else None
    if foundry_dps is not None and s.price:
        return safe_div(float(foundry_dps), s.price, positive_denominator=True)
    return None


@metric("payout_ratio", pillar="shareholder", direction=0, unit="ratio",
        ideal_band=(0.25, 0.60), winsor=(0.0, 3.0))
def payout_ratio(s: SecuritySnapshot) -> float | None:
    """Dividends / net income — **non-monotonic**, with an ideal band of roughly 25-60%.

    Zero means nothing is returned to shareholders; above ~80% the dividend is being paid out of
    a shrinking margin of safety and is at risk. Both ends are worse than the middle, which is why
    this metric declares ``direction=0`` and is scored through the double-sigmoid band rather than
    a monotonic ranking that would crown the most stretched payer.
    """
    dividends = _ttm(s, "dividends_paid")
    if dividends is None:
        return None
    return safe_div(abs(dividends), _ttm(s, "net_income"), positive_denominator=True, cap=3.0)


@metric("fcf_payout_ratio", pillar="shareholder", direction=0, unit="ratio",
        ideal_band=(0.20, 0.60), winsor=(0.0, 3.0))
def fcf_payout_ratio(s: SecuritySnapshot) -> float | None:
    """Dividends / free cash flow — the sustainability test earnings-based payout can miss."""
    dividends = _ttm(s, "dividends_paid")
    if dividends is None:
        return None
    return safe_div(abs(dividends), _ttm(s, "fcf"), positive_denominator=True, cap=3.0)


@metric("buyback_yield", pillar="shareholder", direction=1, unit="ratio", winsor=(-0.5, 0.5))
def buyback_yield(s: SecuritySnapshot) -> float | None:
    """Net buybacks / market cap."""
    buybacks = _ttm(s, "buybacks")
    if buybacks is None:
        return None
    issued = _ttm(s, "stock_issued") or 0.0
    return safe_div(abs(buybacks) - abs(issued), s.market_cap, positive_denominator=True)


@metric("shareholder_yield", pillar="shareholder", direction=1, unit="ratio", winsor=(-0.5, 0.5))
def shareholder_yield(s: SecuritySnapshot) -> float | None:
    """Dividends plus net buybacks, over market cap — total cash returned."""
    dividend = dividend_yield.fn(s) or 0.0
    buyback = buyback_yield.fn(s) or 0.0
    if dividend == 0.0 and buyback == 0.0:
        return None
    return float(dividend + buyback)
