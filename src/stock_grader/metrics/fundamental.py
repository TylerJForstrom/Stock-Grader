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

from ..registry import metric
from ..types import SecuritySnapshot
from .util import cagr, consistency, linear_trend, r_squared_loglinear, safe_div

# Multiples above these are arithmetically true but analytically meaningless; clamping stops a
# company that earned a rounding error from dominating the cross-section.
_MULTIPLE_CAP = 500.0


def _f(snapshot: SecuritySnapshot):
    return snapshot.fundamentals


def _ttm(snapshot: SecuritySnapshot, concept: str) -> float | None:
    f = _f(snapshot)
    return f.ttm(concept) if f is not None else None


# A balance-sheet figure older than this is not the company's current position. Generous on
# purpose: an annual-only filer's balance sheet is legitimately up to fifteen months old.
MAX_BALANCE_AGE_DAYS = 400


def _latest(snapshot: SecuritySnapshot, concept: str) -> float | None:
    f = _f(snapshot)
    if f is None:
        return None
    return f.latest(concept, asof=snapshot.asof, max_age_days=MAX_BALANCE_AGE_DAYS)


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


@metric("pe_trailing", pillar="valuation", direction=-1, unit="x",
        description="Price / trailing twelve-month earnings", winsor=(0.0, _MULTIPLE_CAP))
def pe_trailing(s: SecuritySnapshot) -> float | None:
    """Undefined for loss-makers rather than negative — a negative P/E is not a cheap one."""
    return safe_div(s.market_cap, _ttm(s, "net_income"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_sales", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_sales(s: SecuritySnapshot) -> float | None:
    """Price / trailing revenue. Meaningful even when earnings are negative."""
    return safe_div(s.market_cap, _ttm(s, "revenue"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_book", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_book(s: SecuritySnapshot) -> float | None:
    """Price / shareholders' equity. Undefined when book value is negative."""
    return safe_div(s.market_cap, _latest(s, "equity"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_tangible_book", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_tangible_book(s: SecuritySnapshot) -> float | None:
    """Price / book value excluding goodwill and intangibles."""
    return safe_div(s.market_cap, _latest(s, "tangible_book"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_fcf", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_fcf(s: SecuritySnapshot) -> float | None:
    """Price / free cash flow."""
    return safe_div(s.market_cap, _ttm(s, "fcf"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("price_to_ocf", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def price_to_ocf(s: SecuritySnapshot) -> float | None:
    """Price / cash from operations — harder to manipulate than earnings."""
    return safe_div(s.market_cap, _ttm(s, "cfo"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_ebitda", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_ebitda(s: SecuritySnapshot) -> float | None:
    """Enterprise value / EBITDA — capital-structure neutral, so it compares across leverage."""
    return safe_div(_enterprise_value(s), _ttm(s, "ebitda"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_ebit", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_ebit(s: SecuritySnapshot) -> float | None:
    """Enterprise value / EBIT. Charges companies for depreciation, unlike EV/EBITDA."""
    return safe_div(_enterprise_value(s), _ttm(s, "ebit"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_sales", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_sales(s: SecuritySnapshot) -> float | None:
    """Enterprise value / revenue."""
    return safe_div(_enterprise_value(s), _ttm(s, "revenue"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("ev_to_fcf", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def ev_to_fcf(s: SecuritySnapshot) -> float | None:
    """Enterprise value / free cash flow."""
    return safe_div(_enterprise_value(s), _ttm(s, "fcf"), positive_denominator=True, cap=_MULTIPLE_CAP)


@metric("earnings_yield", pillar="valuation", direction=1, unit="ratio")
def earnings_yield(s: SecuritySnapshot) -> float | None:
    """Earnings / price. The reciprocal of P/E, but defined for loss-makers (it goes negative)."""
    return safe_div(_ttm(s, "net_income"), s.market_cap, positive_denominator=True)


@metric("fcf_yield", pillar="valuation", direction=1, unit="ratio")
def fcf_yield(s: SecuritySnapshot) -> float | None:
    """Free cash flow / market cap."""
    return safe_div(_ttm(s, "fcf"), s.market_cap, positive_denominator=True)


@metric("acquirers_multiple", pillar="valuation", direction=-1, unit="x", winsor=(0.0, _MULTIPLE_CAP))
def acquirers_multiple(s: SecuritySnapshot) -> float | None:
    """EV / operating earnings — Tobias Carlisle's takeover-value screen."""
    return safe_div(_enterprise_value(s), _ttm(s, "operating_income"),
                    positive_denominator=True, cap=_MULTIPLE_CAP)


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
    if f is None or s.price is None or s.shares_outstanding in (None, 0):
        return None
    eps = _ttm(s, "net_income")
    equity = _latest(s, "equity")
    if eps is None or equity is None or eps <= 0 or equity <= 0:
        return None
    eps_ps = eps / s.shares_outstanding
    bvps = equity / s.shares_outstanding
    fair = (22.5 * eps_ps * bvps) ** 0.5
    return safe_div(fair, s.price, positive_denominator=True)


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
    return safe_div(_ttm(s, "net_income"), _latest(s, "equity"), positive_denominator=True)


@metric("roa", pillar="profitability", direction=1, unit="ratio")
def roa(s: SecuritySnapshot) -> float | None:
    """Return on assets."""
    return safe_div(_ttm(s, "net_income"), _latest(s, "assets"), positive_denominator=True)


@metric("roic", pillar="profitability", direction=1, unit="ratio")
def roic(s: SecuritySnapshot) -> float | None:
    """After-tax operating profit / invested capital — the best single measure of business quality."""
    ebit = _ttm(s, "ebit")
    tax = _ttm(s, "income_tax")
    pretax = _ttm(s, "pretax_income")
    if ebit is None:
        return None
    rate = safe_div(tax, pretax, positive_denominator=True)
    rate = 0.21 if rate is None or not (0.0 <= rate <= 0.6) else rate
    return safe_div(ebit * (1 - rate), _latest(s, "invested_capital"), positive_denominator=True)


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
    """Five-year book value per share growth — Buffett's preferred scorecard."""
    return _cagr_metric(s, "equity", 5)


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


@metric("altman_z", pillar="health", direction=1, unit="score", winsor=(-10.0, 20.0))
def altman_z(s: SecuritySnapshot) -> float | None:
    """Altman Z-score for public manufacturers.

    ``1.2*WC/TA + 1.4*RE/TA + 3.3*EBIT/TA + 0.6*MVE/TL + 1.0*Sales/TA``. Below 1.81 is the distress
    zone, above 2.99 the safe zone. Deliberately unavailable for financials, REITs and holding
    companies: the model was never estimated on them and its working-capital and sales-to-assets
    terms are not meaningful there. The sector matrix enforces that.
    """
    assets = _latest(s, "assets")
    if assets is None or assets <= 0:
        return None
    working_capital = _latest(s, "working_capital")
    retained = _latest(s, "retained_earnings")
    ebit = _ttm(s, "ebit")
    liabilities = _latest(s, "liabilities")
    sales = _ttm(s, "revenue")
    cap = s.market_cap
    if None in (working_capital, retained, ebit, liabilities, sales, cap) or liabilities <= 0:
        return None
    return float(
        1.2 * (working_capital / assets)
        + 1.4 * (retained / assets)
        + 3.3 * (ebit / assets)
        + 0.6 * (cap / liabilities)
        + 1.0 * (sales / assets)
    )


@metric("piotroski_f_score", pillar="health", direction=1, unit="score")
def piotroski_f_score(s: SecuritySnapshot) -> float | None:
    """Piotroski F-score: nine binary financial-strength tests, 0-9.

    Profitability (4): positive ROA, positive CFO, rising ROA, CFO exceeding net income (accrual
    quality). Leverage/liquidity (3): falling long-term debt ratio, rising current ratio, no share
    issuance. Efficiency (2): rising gross margin, rising asset turnover.
    """
    f = _f(s)
    if f is None:
        return None
    annual = f.annual
    if annual.empty or len(annual) < 2:
        return None

    def series(concept: str):
        return annual[concept].dropna() if concept in annual.columns else None

    def pair(concept: str):
        values = series(concept)
        if values is None or len(values) < 2:
            return (None, None)
        return (float(values.iloc[-2]), float(values.iloc[-1]))

    points = 0
    counted = 0

    def test(condition: bool | None) -> None:
        nonlocal points, counted
        if condition is None:
            return
        counted += 1
        points += int(bool(condition))

    prev_assets, curr_assets = pair("assets")
    prev_income, curr_income = pair("net_income")
    prev_cfo, curr_cfo = pair("cfo")

    roa_curr = safe_div(curr_income, curr_assets, positive_denominator=True)
    roa_prev = safe_div(prev_income, prev_assets, positive_denominator=True)
    test(None if roa_curr is None else roa_curr > 0)
    test(None if curr_cfo is None else curr_cfo > 0)
    test(None if (roa_curr is None or roa_prev is None) else roa_curr > roa_prev)
    test(None if (curr_cfo is None or curr_income is None) else curr_cfo > curr_income)

    prev_debt, curr_debt = pair("long_term_debt")
    debt_curr = safe_div(curr_debt, curr_assets, positive_denominator=True)
    debt_prev = safe_div(prev_debt, prev_assets, positive_denominator=True)
    test(None if (debt_curr is None or debt_prev is None) else debt_curr < debt_prev)

    prev_ca, curr_ca = pair("current_assets")
    prev_cl, curr_cl = pair("current_liabilities")
    cr_curr = safe_div(curr_ca, curr_cl, positive_denominator=True)
    cr_prev = safe_div(prev_ca, prev_cl, positive_denominator=True)
    test(None if (cr_curr is None or cr_prev is None) else cr_curr > cr_prev)

    prev_shares, curr_shares = pair("shares_diluted")
    test(None if (prev_shares is None or curr_shares is None) else curr_shares <= prev_shares * 1.01)

    prev_gp, curr_gp = pair("gross_profit")
    prev_rev, curr_rev = pair("revenue")
    gm_curr = safe_div(curr_gp, curr_rev, positive_denominator=True)
    gm_prev = safe_div(prev_gp, prev_rev, positive_denominator=True)
    test(None if (gm_curr is None or gm_prev is None) else gm_curr > gm_prev)

    at_curr = safe_div(curr_rev, curr_assets, positive_denominator=True)
    at_prev = safe_div(prev_rev, prev_assets, positive_denominator=True)
    test(None if (at_curr is None or at_prev is None) else at_curr > at_prev)

    if counted < 5:
        return None  # too few components to be a meaningful F-score

    # NOT points * 9 / counted. That linear rescale let a company with only five testable
    # components that passed all five score exactly 9.0 — indistinguishable from one that passed
    # all nine, despite a variance inflated by sqrt(9/5) = 1.34x and a probability of a perfect
    # score of p^5 rather than p^9 (7.8% versus 1.0% at p = 0.6).
    #
    # The pass *proportion* is shrunk toward 0.5 by counted/(counted + k), so a sparse company is
    # pulled toward the middle rather than allowed to reach either extreme. k = 3 is a judgement
    # call: it costs a full-data company almost nothing (9 of 9 still scores 8.4) while capping a
    # five-component company at 7.9.
    shrink_k = 3.0
    proportion = points / counted
    shrunk = 0.5 + (proportion - 0.5) * (counted / (counted + shrink_k))
    return float(shrunk * 9.0)


# ---------------------------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------------------------


@metric("asset_turnover", pillar="efficiency", direction=1, unit="x")
def asset_turnover(s: SecuritySnapshot) -> float | None:
    """Revenue / total assets."""
    return safe_div(_ttm(s, "revenue"), _latest(s, "assets"), positive_denominator=True)


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
    """Dividends paid / market cap."""
    dividends = _ttm(s, "dividends_paid")
    if dividends is None:
        return None
    return safe_div(abs(dividends), s.market_cap, positive_denominator=True)


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
