"""Metrics that only exist for a particular business model.

The sector applicability matrix correctly disables inventory turnover for a bank and gross margin
for a REIT — those quantities are undefined, not missing. But disabling without replacing left
financials graded on a fraction of the evidence: of 65 price-free metrics, an industrial company
got all 65 while a bank got **36**, and the whole efficiency pillar was empty for banks. A
three-metric pillar carries several times the sampling variance of a twelve-metric one, so bank
grades were not so much wrong as uninformative.

Everything here comes from XBRL tags the filers already publish — no new data source. Verified
present for JPMorgan, Wells Fargo, Bank of America, Citigroup and US Bancorp.

Two honest limitations, stated rather than papered over:

* **This is not net interest margin.** NIM divides net interest income by *average earning assets*,
  and no XBRL tag reports earning assets. Dividing by total assets is a related but different
  quantity, so it is named for what it computes.
* **Tier 1 and CET1 are not tagged.** Tangible common equity over tangible assets is a widely used
  proxy and nothing more; regulatory capital ratios live in the text of the filing, not the XBRL.
"""

from __future__ import annotations

from ..registry import metric
from ..types import SecuritySnapshot
from .util import safe_div

__all__ = [
    "allowance_coverage",
    "deposits_to_assets",
    "efficiency_ratio",
    "fee_income_share",
    "funds_from_operations",
    "loans_to_deposits",
    "net_interest_income_to_assets",
    "price_to_ffo",
    "provision_burden",
    "tangible_common_equity_ratio",
]


def _ttm(s: SecuritySnapshot, concept: str) -> float | None:
    from .fundamental import _ttm as _bounded_ttm

    return _bounded_ttm(s, concept)


def _latest(s: SecuritySnapshot, concept: str) -> float | None:
    from .fundamental import MAX_BALANCE_AGE_DAYS

    f = s.fundamentals
    if f is None:
        return None
    return f.latest(concept, asof=s.asof, max_age_days=MAX_BALANCE_AGE_DAYS)


def _bank_revenue(s: SecuritySnapshot) -> float | None:
    """Net interest income plus fee income — the basis banks are actually compared on."""
    nii = _ttm(s, "net_interest_income")
    fees = _ttm(s, "noninterest_income")
    if nii is None or fees is None:
        return None
    total = nii + fees
    return total if total > 0 else None


# ---------------------------------------------------------------------------------------------
# Banks
# ---------------------------------------------------------------------------------------------


@metric("efficiency_ratio", pillar="efficiency", direction=-1, unit="ratio", winsor=(0.0, 3.0))
def efficiency_ratio(s: SecuritySnapshot) -> float | None:
    """Noninterest expense over net revenue — the headline cost measure for a bank.

    Lower is better: it is the cost of producing a dollar of revenue. Well-run large banks run in
    the 0.50-0.60 range; above ~0.70 is a franchise with a cost problem. This is the single most
    watched operating ratio in bank analysis and the closest thing a bank has to an operating margin.
    """
    return safe_div(_ttm(s, "noninterest_expense"), _bank_revenue(s),
                    positive_denominator=True, cap=3.0)


@metric("net_interest_income_to_assets", pillar="profitability", direction=1, unit="ratio")
def net_interest_income_to_assets(s: SecuritySnapshot) -> float | None:
    """Net interest income over total assets.

    Deliberately **not** called net interest margin: NIM divides by average *earning* assets, which
    no XBRL tag reports. Total assets include cash and premises that earn nothing, so this runs
    below a bank's published NIM and is not comparable with one. It is still a sound
    cross-sectional measure because the distortion is similar across banks.
    """
    return safe_div(_ttm(s, "net_interest_income"), _latest(s, "assets"), positive_denominator=True)


@metric("fee_income_share", pillar="quality", direction=1, unit="ratio")
def fee_income_share(s: SecuritySnapshot) -> float | None:
    """Noninterest income as a share of net revenue.

    Fee income is less rate-sensitive than spread income, so a higher share generally means more
    stable earnings through a rate cycle — which is why it sits in the quality pillar rather than
    profitability.
    """
    return safe_div(_ttm(s, "noninterest_income"), _bank_revenue(s), positive_denominator=True)


@metric("deposits_to_assets", pillar="health", direction=1, unit="ratio")
def deposits_to_assets(s: SecuritySnapshot) -> float | None:
    """Deposits over total assets — the cheapest and stickiest funding a bank has.

    A deposit-funded bank is far less exposed to wholesale funding markets closing. Higher is
    better, which is the opposite of how a leverage ratio reads for an industrial, and one reason
    the standard solvency metrics do not transfer.
    """
    return safe_div(_latest(s, "deposits"), _latest(s, "assets"), positive_denominator=True)


@metric("loans_to_deposits", pillar="health", direction=0, unit="ratio", ideal_band=(0.60, 0.90),
        winsor=(0.0, 3.0))
def loans_to_deposits(s: SecuritySnapshot) -> float | None:
    """Loans over deposits — **non-monotonic**, with an ideal band around 60-90%.

    Above roughly 1.0 the bank is lending more than it takes in deposits and must fund the gap in
    wholesale markets, which is precisely the vulnerability that closed Silicon Valley Bank. Far
    below 0.6 the bank is sitting on deposits it cannot deploy, which is safe but unprofitable.
    Both ends are worse than the middle.
    """
    return safe_div(_latest(s, "loans"), _latest(s, "deposits"),
                    positive_denominator=True, cap=3.0)


@metric("allowance_coverage", pillar="health", direction=1, unit="ratio", winsor=(0.0, 0.5))
def allowance_coverage(s: SecuritySnapshot) -> float | None:
    """Loan-loss allowance over gross loans — the cushion already set aside against credit losses."""
    return safe_div(_latest(s, "loan_loss_allowance"), _latest(s, "loans"),
                    positive_denominator=True, cap=0.5)


@metric("provision_burden", pillar="quality", direction=-1, unit="ratio", winsor=(-0.5, 1.0))
def provision_burden(s: SecuritySnapshot) -> float | None:
    """Loan-loss provision over net revenue — how much of this year's revenue credit is consuming.

    Genuinely negative in a release year, when a bank reverses provisions it no longer needs, so the
    winsor floor allows it rather than treating a release as an error.
    """
    return safe_div(_ttm(s, "loan_loss_provision"), _bank_revenue(s), cap=1.0)


@metric("tangible_common_equity_ratio", pillar="health", direction=1, unit="ratio",
        winsor=(-0.2, 0.5))
def tangible_common_equity_ratio(s: SecuritySnapshot) -> float | None:
    """Tangible common equity over tangible assets — a proxy for regulatory capital.

    A **proxy**, not Tier 1 or CET1: those are defined by risk-weighting rules and are reported in
    the narrative of a filing rather than in XBRL, so they cannot be read from this data at all.
    TCE is the market's usual stand-in and moves with the same forces, but a bank can be adequately
    capitalised on a risk-weighted basis while looking thin here, and vice versa.
    """
    equity = _latest(s, "equity")
    assets = _latest(s, "assets")
    if equity is None or assets is None:
        return None
    goodwill = _latest(s, "goodwill") or 0.0
    intangibles = _latest(s, "intangibles") or 0.0
    preferred = _latest(s, "preferred_equity") or 0.0
    tangible_equity = equity - goodwill - intangibles - preferred
    tangible_assets = assets - goodwill - intangibles
    return safe_div(tangible_equity, tangible_assets, positive_denominator=True)


# ---------------------------------------------------------------------------------------------
# REITs
# ---------------------------------------------------------------------------------------------


@metric("funds_from_operations", pillar="profitability", direction=1, unit="usd")
def funds_from_operations(s: SecuritySnapshot) -> tuple[float, dict] | None:
    """FFO — the REIT earnings measure, **reconstructed** because nobody tags it.

    ``FFO = net income to common + real-estate depreciation - gains on property sales + impairments``

    Checked against Simon Property, Realty Income, Prologis and American Tower: **none** of them
    tags ``FundsFromOperations`` in XBRL, so any implementation reading it directly would report
    100% missing. It is reconstructed from components instead.

    Why REITs need it at all: property depreciation is an enormous non-cash charge against a
    portfolio that is typically appreciating, so GAAP net income systematically understates a REIT's
    economic earnings. That is also why the sector matrix disables P/E for REITs — the denominator
    is not a meaningful measure of what the business earns.

    The result is labelled a reconstruction and sanity-checked against net income; a ratio outside
    1.0-6.0x means the components did not assemble sensibly and it returns ``None`` rather than a
    number that looks authoritative.
    """
    income = _ttm(s, "income_to_common")
    if income is None:
        income = _ttm(s, "net_income")
    depreciation = _ttm(s, "depreciation_amortization")
    if income is None or depreciation is None:
        return None
    gains = _ttm(s, "gain_on_property_sale") or 0.0
    impairment = _ttm(s, "real_estate_impairment") or 0.0
    ffo = income + depreciation - gains + impairment
    if ffo <= 0:
        return None
    if income > 0:
        ratio = ffo / income
        if not 1.0 <= ratio <= 6.0:
            return None
    return (
        float(ffo),
        {
            "income_to_common": income,
            "depreciation": depreciation,
            "gain_on_sale": gains,
            "impairment": impairment,
            "reconstructed": 1.0,
        },
    )


@metric("price_to_ffo", pillar="valuation", direction=-1, unit="x", winsor=(0.0, 100.0))
def price_to_ffo(s: SecuritySnapshot) -> float | None:
    """Market cap over funds from operations — the REIT equivalent of a P/E.

    Typical range is roughly 10-25x. This is what the sector matrix disables ``pe_trailing`` in
    favour of: depreciation dominates a REIT's income statement, so a P/E computed on GAAP earnings
    says more about the age of the portfolio than about its value.
    """
    result = funds_from_operations.fn(s)
    if result is None:
        return None
    ffo = result[0] if isinstance(result, tuple) else result
    return safe_div(s.market_cap, ffo, positive_denominator=True, cap=100.0)
