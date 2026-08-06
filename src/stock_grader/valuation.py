"""Transparent, assumption-led cash-flow valuation.

Valuation is intentionally separate from the historical grade.  A backward-looking factor score
cannot become an intrinsic value without forecasts, and silently inventing those forecasts would
make the output less trustworthy.  The functions here therefore expose every assumption and
return an illustrative range plus the growth rate implied by the current price.

The cash flow available from the current SEC model is ``cash from operations - capital
expenditure``. Because cash from operations is after interest, this is a *levered cash-flow
proxy*, but it is not canonical FCFE: debt principal issued/repaid is not included. The module
therefore labels the result as illustrative and discounts it at a required equity return. It does
**not** subtract net debt or call the discount rate WACC; doing so would mix an after-interest
numerator with an enterprise-value denominator. A full valuation should replace this proxy with
forecast FCFE or forecast FCFF paired with a consistent discount rate.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from numbers import Integral

import pandas as pd

from .types import SectorClass, SecuritySnapshot

__all__ = [
    "DCFResult",
    "DCFScenario",
    "ValuationAnalysis",
    "build_valuation_analysis",
    "equity_cash_flow_value",
    "implied_growth_rate",
]

_MAX_INPUT_AGE_DAYS = 400
_HIGH_TERMINAL_VALUE_CONCENTRATION = 0.75
_REVERSE_GROWTH_BOUNDS = (-0.50, 1.00)

# Discount-rate construction. A hardcoded 10% applied identically to a
# regulated utility and a micro-cap biotech was the single largest unforced
# valuation error: the rate now builds from the observed risk-free rate plus a
# documented long-run equity risk premium, and the bear/base/bull scenarios
# vary the rate as well as growth — discount-rate uncertainty dominates growth
# uncertainty in five-year-plus-terminal structures.
_DEFAULT_EQUITY_RISK_PREMIUM = 0.05
_LEGACY_DISCOUNT_RATE = 0.10
_SCENARIO_RATE_SPREAD = 0.015  # bear +150bp, bull -150bp


def derive_discount_rate(
    risk_free_rate: float,
    *,
    equity_risk_premium: float = _DEFAULT_EQUITY_RISK_PREMIUM,
    beta: float | None = None,
) -> float:
    """Required equity return: risk-free + (shrunk beta) x equity risk premium.

    Beta, when supplied, is Blume-shrunk toward 1 (``0.5 + 0.5*beta``) and
    clipped to [0, 3] first — raw historical betas are noisy and mean-revert.
    Without a beta the market beta of 1 applies and the rate is rf + ERP.
    """
    if not math.isfinite(risk_free_rate) or not -0.05 <= risk_free_rate <= 0.20:
        raise ValueError("risk_free_rate must be a plausible annual decimal rate")
    if not math.isfinite(equity_risk_premium) or not 0.0 <= equity_risk_premium <= 0.10:
        raise ValueError("equity_risk_premium must be between 0 and 10%")
    if beta is None or not math.isfinite(beta):
        shrunk = 1.0
    else:
        shrunk = 0.5 + 0.5 * min(3.0, max(0.0, float(beta)))
    return float(risk_free_rate + shrunk * equity_risk_premium)


def _latest_risk_free(snapshot: SecuritySnapshot) -> float | None:
    """Newest plausible annual risk-free rate from the snapshot's series."""
    series = getattr(snapshot, "risk_free", None)
    if series is None or len(series) == 0:
        return None
    values = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    values = values[(values > -0.05) & (values < 0.20)]
    if values.empty:
        return None
    return float(values.iloc[-1])


@dataclass(frozen=True, slots=True)
class DCFScenario:
    """All assumptions required for one equity cash-flow scenario.

    ``annual_dilution_rate`` changes the share count during the explicit period.
    ``terminal_dilution_rate=None`` continues that rate in perpetuity; pass an explicit terminal
    rate (commonly zero) when the analyst expects dilution or buybacks to fade by the terminal year.
    """

    name: str
    growth_rate: float
    discount_rate: float = 0.10
    terminal_growth_rate: float = 0.025
    years: int = 5
    annual_dilution_rate: float = 0.0
    terminal_dilution_rate: float | None = None


@dataclass(slots=True)
class DCFResult:
    """Reconciled result for one scenario."""

    scenario: DCFScenario
    value_per_share: float
    current_price: float | None
    upside_downside: float | None
    forecast_cash_flows: list[float]
    forecast_shares: list[float]
    forecast_cash_flows_per_share: list[float]
    present_value_explicit: float
    present_value_terminal: float
    terminal_cash_flow_per_share: float
    terminal_per_share_growth_rate: float
    terminal_value_concentration: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ValuationAnalysis:
    """Scenario range and reverse-DCF output for an analyst report."""

    method: str = "cfo_minus_capex_equity_proxy_dcf"
    available: bool = False
    base_fcf: float | None = None
    shares_outstanding: float | None = None
    current_price: float | None = None
    scenarios: list[DCFResult] = field(default_factory=list)
    implied_five_year_growth: float | None = None
    implied_explicit_period_growth: float | None = None
    scenario_values_ordered: bool | None = None
    assumptions: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _validate(
    *,
    base_fcf: float,
    shares_outstanding: float,
    growth_rate: float,
    discount_rate: float,
    terminal_growth_rate: float,
    years: int,
    annual_dilution_rate: float,
    terminal_dilution_rate: float | None,
) -> float:
    effective_terminal_dilution = (
        annual_dilution_rate if terminal_dilution_rate is None else terminal_dilution_rate
    )
    values = {
        "base_fcf": base_fcf,
        "shares_outstanding": shares_outstanding,
        "growth_rate": growth_rate,
        "discount_rate": discount_rate,
        "terminal_growth_rate": terminal_growth_rate,
        "annual_dilution_rate": annual_dilution_rate,
        "terminal_dilution_rate": effective_terminal_dilution,
    }
    for name, value in values.items():
        try:
            finite = math.isfinite(value)
        except TypeError as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not finite:
            raise ValueError(f"{name} must be finite")
    if base_fcf <= 0:
        raise ValueError("base_fcf must be positive")
    if shares_outstanding <= 0:
        raise ValueError("shares_outstanding must be positive")
    if isinstance(years, bool) or not isinstance(years, Integral):
        raise ValueError("years must be an integer")
    if years < 1 or years > 50:
        raise ValueError("years must be between 1 and 50")
    if discount_rate <= 0.0:
        raise ValueError("discount_rate must be positive")
    if discount_rate <= terminal_growth_rate:
        raise ValueError("discount_rate must be greater than terminal_growth_rate")
    if growth_rate <= -1.0 or terminal_growth_rate <= -1.0:
        raise ValueError("rates must be greater than -100%")
    if annual_dilution_rate <= -1.0:
        raise ValueError("annual_dilution_rate must be greater than -100%")
    if effective_terminal_dilution <= -1.0:
        raise ValueError("terminal_dilution_rate must be greater than -100%")

    terminal_per_share_growth = (1.0 + terminal_growth_rate) / (
        1.0 + effective_terminal_dilution
    ) - 1.0
    if discount_rate <= terminal_per_share_growth:
        raise ValueError(
            "discount_rate must be greater than terminal per-share growth after dilution"
        )
    return float(effective_terminal_dilution)


def _validate_current_price(current_price: float) -> float:
    try:
        price = float(current_price)
    except (TypeError, ValueError) as exc:
        raise ValueError("current_price must be positive and finite") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError("current_price must be positive and finite")
    return price


def _aligned_trailing_fcf(
    snapshot: SecuritySnapshot,
) -> tuple[float | None, list[str], str | None]:
    """Return CFO minus capex from the same four current fiscal quarters.

    ``Fundamentals.ttm("fcf")`` deliberately resolves each component independently. That is useful
    for broad metric coverage, but it is too permissive for a valuation anchor: a current CFO
    window and an older capex window can produce a plausible-looking cash flow that never existed
    in one trailing year.
    """

    fundamentals = snapshot.fundamentals
    if fundamentals is None:
        return None, [], "fundamentals unavailable"
    frame = fundamentals.quarterly
    if frame.empty or not {"cfo", "capex"} <= set(frame.columns):
        return None, [], "cash from operations and capex are both required"

    selected = frame.loc[:, ["cfo", "capex"]].copy()
    parsed_index = pd.to_datetime(selected.index, errors="coerce")
    selected = selected.loc[parsed_index.notna()]
    selected.index = parsed_index[parsed_index.notna()]
    selected = selected[~selected.index.duplicated(keep="last")].sort_index()
    cutoff = pd.Timestamp(snapshot.asof)
    selected = selected.loc[selected.index <= cutoff]
    if selected.empty:
        return None, [], "no cash-flow periods exist on or before the valuation date"

    latest_periods = []
    for concept in ("cfo", "capex"):
        series = selected[concept].dropna()
        if series.empty:
            return None, [], f"{concept} is unavailable"
        latest_periods.append(series.index[-1])
    if latest_periods[0] != latest_periods[1]:
        return None, [], "cash from operations and capex do not share the latest fiscal period"

    aligned = selected.dropna(subset=["cfo", "capex"]).iloc[-4:]
    if len(aligned) != 4:
        return None, [], "fewer than four aligned cash-flow quarters are available"
    values = aligned.to_numpy().ravel()
    if any(not math.isfinite(float(value)) for value in values):
        return None, [], "cash-flow inputs contain a non-finite value"
    if (aligned["capex"] < 0).any():
        return None, [], "capex has an invalid sign under the positive-outflow data convention"

    dates = list(aligned.index)
    gaps = [(right - left).days for left, right in pairwise(dates)]
    if any(gap < 60 or gap > 130 for gap in gaps):
        return None, [], "cash-flow quarters are not consecutive"
    if (cutoff - dates[-1]).days > _MAX_INPUT_AGE_DAYS:
        return None, [], "the latest aligned cash-flow quarter is stale"

    base_fcf = float(aligned["cfo"].sum() - aligned["capex"].sum())
    periods = [period.date().isoformat() for period in dates]
    return base_fcf, periods, None


def _valuation_share_count(
    snapshot: SecuritySnapshot,
) -> tuple[float | None, str, str | None]:
    """Choose a defensible current per-share denominator.

    A period-end basic count can understate claims from in-the-money awards. When the SEC loader
    supplies a plausibly scaled weighted-average diluted count, the larger of the two is the safer
    denominator. This is still a proxy for a true treasury-stock-method fully diluted count.
    """

    reported = snapshot.shares_outstanding
    try:
        reported_value = float(reported) if reported is not None else None
    except (TypeError, ValueError):
        reported_value = None
    if reported_value is None or not math.isfinite(reported_value) or reported_value <= 0:
        return None, "unavailable", "usable share count unavailable"

    diluted = snapshot.meta.get("shares_diluted")
    try:
        diluted_value = float(diluted) if diluted is not None else None
    except (TypeError, ValueError):
        diluted_value = None
    if diluted_value is None or not math.isfinite(diluted_value) or diluted_value <= 0:
        return reported_value, "reported_period_end_shares", None

    ratio = diluted_value / reported_value
    if ratio < 0.5 or ratio > 2.0:
        return (
            reported_value,
            "reported_period_end_shares",
            "diluted share count failed a scale plausibility check and was not used",
        )
    if diluted_value > reported_value:
        return (
            diluted_value,
            "larger_of_reported_and_weighted_average_diluted_shares",
            None,
        )
    return reported_value, "reported_period_end_shares", None


def _comparison_price(
    snapshot: SecuritySnapshot,
) -> tuple[float | None, str, str | None]:
    """Return only a point price suitable for upside and reverse-DCF comparisons."""

    source = snapshot.meta.get("price_source")
    rejection = snapshot.meta.get("valuation_price_rejected")
    if source == "public_float_lower_bound" or rejection == "public_float_lower_bound":
        return (
            None,
            "lower_bound_omitted",
            "price is only a public-float lower bound; reverse DCF and upside are omitted",
        )
    if rejection in {"split_basis_mismatch", "split_basis_unverified"}:
        return (
            None,
            str(rejection),
            (
                "historical price/share split basis is not valuation-safe; reverse DCF and upside "
                "are omitted"
            ),
        )
    if snapshot.price is None:
        return None, "unavailable", "current price unavailable; reverse DCF and upside are omitted"
    try:
        price = _validate_current_price(snapshot.price)
    except ValueError:
        return (
            None,
            "invalid",
            "current price is not positive and finite; reverse DCF and upside are omitted",
        )

    if snapshot.meta.get("price_is_adjusted", False):
        return (
            None,
            "adjusted_price_omitted",
            "only an adjusted historical price is available; reverse DCF and upside are omitted",
        )
    if snapshot.synthetic_prices and source in (None, "synthetic"):
        return (
            None,
            "synthetic_omitted",
            "synthetic price is not market evidence; reverse DCF and upside are omitted",
        )
    return price, "usable", None


def equity_cash_flow_value(
    *,
    base_fcf: float,
    shares_outstanding: float,
    scenario: DCFScenario,
    current_price: float | None = None,
) -> DCFResult:
    """Discount the projected after-interest cash-flow proxy on a per-share basis."""

    terminal_dilution_rate = _validate(
        base_fcf=base_fcf,
        shares_outstanding=shares_outstanding,
        growth_rate=scenario.growth_rate,
        discount_rate=scenario.discount_rate,
        terminal_growth_rate=scenario.terminal_growth_rate,
        years=scenario.years,
        annual_dilution_rate=scenario.annual_dilution_rate,
        terminal_dilution_rate=scenario.terminal_dilution_rate,
    )
    price = _validate_current_price(current_price) if current_price is not None else None

    base_fcf = float(base_fcf)
    shares_outstanding = float(shares_outstanding)
    growth_rate = float(scenario.growth_rate)
    discount_rate = float(scenario.discount_rate)
    terminal_growth_rate = float(scenario.terminal_growth_rate)
    annual_dilution_rate = float(scenario.annual_dilution_rate)
    years = int(scenario.years)

    cash_flows: list[float] = []
    shares: list[float] = []
    cash_flows_per_share: list[float] = []
    present_value_explicit = 0.0
    try:
        for year in range(1, years + 1):
            cash_flow = base_fcf * (1.0 + growth_rate) ** year
            share_count = shares_outstanding * (1.0 + annual_dilution_rate) ** year
            cash_flow_per_share = cash_flow / share_count
            discount_factor = (1.0 + discount_rate) ** year
            if not all(
                math.isfinite(value)
                for value in (cash_flow, share_count, cash_flow_per_share, discount_factor)
            ):
                raise ValueError("scenario assumptions produce a non-finite projection")
            cash_flows.append(float(cash_flow))
            shares.append(float(share_count))
            cash_flows_per_share.append(float(cash_flow_per_share))
            present_value_explicit += cash_flow_per_share / discount_factor
    except OverflowError as exc:
        raise ValueError("scenario assumptions overflow the projection horizon") from exc

    terminal_per_share_growth = (1.0 + terminal_growth_rate) / (1.0 + terminal_dilution_rate) - 1.0
    terminal_cash_flow_per_share = cash_flows_per_share[-1] * (1.0 + terminal_per_share_growth)
    terminal_value_per_share = terminal_cash_flow_per_share / (
        discount_rate - terminal_per_share_growth
    )
    present_value_terminal = terminal_value_per_share / (1.0 + discount_rate) ** years
    value = present_value_explicit + present_value_terminal
    if not all(
        math.isfinite(item)
        for item in (
            present_value_explicit,
            terminal_cash_flow_per_share,
            present_value_terminal,
            value,
        )
    ):
        raise ValueError("scenario assumptions produce a non-finite valuation")
    terminal_value_concentration = present_value_terminal / value
    upside = value / price - 1.0 if price is not None else None

    return DCFResult(
        scenario=scenario,
        value_per_share=float(value),
        current_price=price,
        upside_downside=float(upside) if upside is not None else None,
        forecast_cash_flows=cash_flows,
        forecast_shares=shares,
        forecast_cash_flows_per_share=cash_flows_per_share,
        present_value_explicit=float(present_value_explicit),
        present_value_terminal=float(present_value_terminal),
        terminal_cash_flow_per_share=float(terminal_cash_flow_per_share),
        terminal_per_share_growth_rate=float(terminal_per_share_growth),
        terminal_value_concentration=float(terminal_value_concentration),
    )


def implied_growth_rate(
    *,
    current_price: float,
    base_fcf: float,
    shares_outstanding: float,
    discount_rate: float = 0.10,
    terminal_growth_rate: float = 0.025,
    years: int = 5,
    annual_dilution_rate: float = 0.0,
    terminal_dilution_rate: float | None = None,
    lower: float = -0.50,
    upper: float = 1.00,
    tolerance: float = 1e-7,
) -> float | None:
    """Solve the constant five-year FCF growth rate implied by the market price.

    Returns ``None`` when the market price lies outside the values spanned by the stated search
    range.  That is more honest than returning a clipped growth rate that appears to be a solution.
    """

    current_price = _validate_current_price(current_price)
    try:
        lower = float(lower)
        upper = float(upper)
        tolerance = float(tolerance)
    except (TypeError, ValueError) as exc:
        raise ValueError("reverse-DCF bounds and tolerance must be finite numbers") from exc
    if not all(math.isfinite(value) for value in (lower, upper, tolerance)):
        raise ValueError("reverse-DCF bounds and tolerance must be finite")
    if lower >= upper:
        raise ValueError("lower must be less than upper")
    if lower <= -1.0:
        raise ValueError("lower must be greater than -100%")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")

    # Validate common assumptions before entering the solver. Endpoint evaluation below should
    # answer only whether the observed price is bracketed, not incidentally validate the model.
    for endpoint in (lower, upper):
        _validate(
            base_fcf=base_fcf,
            shares_outstanding=shares_outstanding,
            growth_rate=endpoint,
            discount_rate=discount_rate,
            terminal_growth_rate=terminal_growth_rate,
            years=years,
            annual_dilution_rate=annual_dilution_rate,
            terminal_dilution_rate=terminal_dilution_rate,
        )

    def difference(growth: float) -> float:
        result = equity_cash_flow_value(
            base_fcf=base_fcf,
            shares_outstanding=shares_outstanding,
            scenario=DCFScenario(
                name="reverse",
                growth_rate=growth,
                discount_rate=discount_rate,
                terminal_growth_rate=terminal_growth_rate,
                years=years,
                annual_dilution_rate=annual_dilution_rate,
                terminal_dilution_rate=terminal_dilution_rate,
            ),
        )
        return result.value_per_share - current_price

    left, right = difference(lower), difference(upper)
    price_tolerance = tolerance * max(1.0, current_price)
    if abs(left) <= price_tolerance:
        return lower
    if abs(right) <= price_tolerance:
        return upper
    if left > right:
        raise RuntimeError("reverse-DCF value is not monotonic in the growth assumption")
    if (left < 0 and right < 0) or (left > 0 and right > 0):
        return None
    for _ in range(100):
        midpoint = (lower + upper) / 2.0
        value = difference(midpoint)
        if abs(value) <= price_tolerance or (upper - lower) <= tolerance:
            return float(midpoint)
        if value >= 0:
            upper = midpoint
        else:
            lower, left = midpoint, value
    return float((lower + upper) / 2.0)


def _normalise_build_assumptions(
    *,
    growth_rates: tuple[float, float, float],
    discount_rate: float,
    terminal_growth_rate: float,
    years: int,
    annual_dilution_rate: float,
    terminal_dilution_rate: float | None,
) -> tuple[tuple[float, float, float], float, float, int, float, float | None, float]:
    try:
        raw_growth_rates = tuple(float(value) for value in growth_rates)
        discount_rate = float(discount_rate)
        terminal_growth_rate = float(terminal_growth_rate)
        annual_dilution_rate = float(annual_dilution_rate)
        terminal_dilution_rate = (
            float(terminal_dilution_rate) if terminal_dilution_rate is not None else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("valuation assumptions must be finite numbers") from exc
    if len(raw_growth_rates) != 3:
        raise ValueError("growth_rates must contain exactly bear, base, and bull assumptions")
    growth_tuple = (
        raw_growth_rates[0],
        raw_growth_rates[1],
        raw_growth_rates[2],
    )
    if not growth_tuple[0] < growth_tuple[1] < growth_tuple[2]:
        raise ValueError("growth_rates must be strictly ordered bear < base < bull")

    effective_terminal_dilution = 0.0
    for growth_rate in growth_tuple:
        effective_terminal_dilution = _validate(
            base_fcf=1.0,
            shares_outstanding=1.0,
            growth_rate=growth_rate,
            discount_rate=discount_rate,
            terminal_growth_rate=terminal_growth_rate,
            years=years,
            annual_dilution_rate=annual_dilution_rate,
            terminal_dilution_rate=terminal_dilution_rate,
        )
    return (
        growth_tuple,
        discount_rate,
        terminal_growth_rate,
        int(years),
        annual_dilution_rate,
        terminal_dilution_rate,
        effective_terminal_dilution,
    )


def build_valuation_analysis(
    snapshot: SecuritySnapshot,
    *,
    growth_rates: tuple[float, float, float] = (-0.02, 0.05, 0.12),
    discount_rate: float | None = None,
    terminal_growth_rate: float = 0.025,
    years: int = 5,
    annual_dilution_rate: float = 0.0,
    terminal_dilution_rate: float | None = None,
    beta: float | None = None,
) -> ValuationAnalysis:
    """Build bear/base/bull and reverse-DCF views from one snapshot.

    ``discount_rate=None`` (the default) derives the required equity return from
    the snapshot's risk-free series plus a documented equity risk premium,
    optionally beta-adjusted; an explicit rate is honored verbatim. Scenarios
    vary the discount rate alongside growth (bear +150bp, bull -150bp).

    Banks, insurers, REITs, and investment holding companies are refused because after-interest
    corporate FCF is not the right valuation numerator for those business models. Dedicated
    residual-income, AFFO/NAV, or sum-of-the-parts models should be used instead.
    """
    risk_free_rate = _latest_risk_free(snapshot)
    rate_warnings: list[str] = []
    if discount_rate is None:
        if risk_free_rate is not None:
            discount_rate = derive_discount_rate(risk_free_rate, beta=beta)
            rate_derivation = "risk_free_plus_equity_risk_premium"
        else:
            discount_rate = _LEGACY_DISCOUNT_RATE
            rate_derivation = "legacy_default_no_risk_free_series"
            rate_warnings.append(
                "no risk-free series available: discount rate fell back to the "
                f"legacy {_LEGACY_DISCOUNT_RATE:.0%} assumption"
            )
    else:
        rate_derivation = "analyst_supplied"
    if risk_free_rate is not None and terminal_growth_rate > risk_free_rate + 1e-3:
        rate_warnings.append(
            f"terminal growth {terminal_growth_rate:.1%} exceeds the risk-free rate "
            f"{risk_free_rate:.1%}: a perpetuity cannot outgrow the economy's "
            "risk-free benchmark (Damodaran cap); treat the terminal values as optimistic"
        )

    (
        growth_rates,
        discount_rate,
        terminal_growth_rate,
        years,
        annual_dilution_rate,
        terminal_dilution_rate,
        effective_terminal_dilution,
    ) = _normalise_build_assumptions(
        growth_rates=growth_rates,
        discount_rate=discount_rate,
        terminal_growth_rate=terminal_growth_rate,
        years=years,
        annual_dilution_rate=annual_dilution_rate,
        terminal_dilution_rate=terminal_dilution_rate,
    )
    analysis = ValuationAnalysis(
        assumptions={
            "growth_rates": list(growth_rates),
            "discount_rate": discount_rate,
            "discount_rate_definition": "required_equity_return",
            "discount_rate_derivation": rate_derivation,
            "risk_free_rate": risk_free_rate,
            "equity_risk_premium": (
                _DEFAULT_EQUITY_RISK_PREMIUM
                if rate_derivation == "risk_free_plus_equity_risk_premium"
                else None
            ),
            "beta_assumption": beta if beta is not None else "market (1.0)",
            "scenario_discount_rate_spread": _SCENARIO_RATE_SPREAD,
            "terminal_growth_rate": terminal_growth_rate,
            "years": years,
            "annual_dilution_rate": annual_dilution_rate,
            "terminal_dilution_rate": effective_terminal_dilution,
            "terminal_dilution_convention": (
                "continues_explicit_rate"
                if terminal_dilution_rate is None
                else "analyst_supplied_terminal_rate"
            ),
            "scenario_order": ["bear", "base", "bull"],
            "reverse_growth_bounds": list(_REVERSE_GROWTH_BOUNDS),
            "cash_flow_definition": (
                "cash_from_operations_minus_capex_after_interest; levered_proxy_not_canonical_fcfe"
            ),
            "interpretation": "illustrative_scenarios_not_analyst_forecasts",
        },
    )
    analysis.warnings.extend(rate_warnings)

    if snapshot.sector in (
        SectorClass.BANK,
        SectorClass.INSURANCE,
        SectorClass.REIT,
        SectorClass.HOLDING,
    ):
        analysis.warnings.append(
            f"cash-flow proxy DCF is not appropriate for {snapshot.sector.value}; "
            "use a business-model-specific valuation"
        )
        return analysis
    if snapshot.fundamentals is None:
        analysis.warnings.append("fundamentals unavailable")
        return analysis

    statement_currency = str(snapshot.fundamentals.currency).strip().upper()
    security_currency = str(snapshot.currency).strip().upper()
    if not statement_currency or not security_currency or statement_currency != security_currency:
        analysis.warnings.append(
            "cash-flow and per-share price currencies are not demonstrably consistent; "
            "no DCF value was produced"
        )
        return analysis
    analysis.assumptions["cash_flow_currency"] = statement_currency

    base_fcf, base_periods, base_error = _aligned_trailing_fcf(snapshot)
    if base_fcf is None:
        analysis.warnings.append(
            f"valuation base unavailable: {base_error}; no DCF value was produced"
        )
        return analysis
    if not math.isfinite(base_fcf) or base_fcf <= 0:
        analysis.warnings.append(
            "positive trailing free cash flow unavailable; no DCF value was produced"
        )
        return analysis

    valuation_shares, share_definition, share_warning = _valuation_share_count(snapshot)
    if valuation_shares is None:
        analysis.warnings.append(share_warning or "usable share count unavailable")
        return analysis
    if share_warning is not None:
        analysis.warnings.append(share_warning)

    analysis.base_fcf = float(base_fcf)
    analysis.shares_outstanding = valuation_shares
    analysis.assumptions["base_fcf_periods"] = base_periods
    analysis.assumptions["share_count_definition"] = share_definition

    comparison_price, price_status, price_warning = _comparison_price(snapshot)
    analysis.current_price = comparison_price
    analysis.assumptions["comparison_price_status"] = price_status
    if price_warning is not None:
        analysis.warnings.append(price_warning)

    names = ("bear", "base", "bull")
    # Bear pairs low growth with a HIGHER required return, bull the reverse —
    # varying only growth at a fixed rate understates the true scenario spread.
    # The bull rate is floored above terminal growth so the perpetuity stays valid.
    scenario_rates = {
        "bear": discount_rate + _SCENARIO_RATE_SPREAD,
        "base": discount_rate,
        "bull": max(discount_rate - _SCENARIO_RATE_SPREAD, terminal_growth_rate + 0.005),
    }
    analysis.assumptions["scenario_discount_rates"] = {
        name: round(rate, 6) for name, rate in scenario_rates.items()
    }
    for name, growth in zip(names, growth_rates, strict=True):
        scenario = DCFScenario(
            name=name,
            growth_rate=float(growth),
            discount_rate=scenario_rates[name],
            terminal_growth_rate=terminal_growth_rate,
            years=years,
            annual_dilution_rate=annual_dilution_rate,
            terminal_dilution_rate=terminal_dilution_rate,
        )
        analysis.scenarios.append(
            equity_cash_flow_value(
                base_fcf=analysis.base_fcf,
                shares_outstanding=valuation_shares,
                scenario=scenario,
                current_price=comparison_price,
            )
        )

    scenario_values = [result.value_per_share for result in analysis.scenarios]
    analysis.scenario_values_ordered = all(
        lower_value < upper_value for lower_value, upper_value in pairwise(scenario_values)
    )
    if not analysis.scenario_values_ordered:
        raise RuntimeError("bear/base/bull assumptions did not produce strictly ordered values")

    concentrated = [
        f"{result.scenario.name} {result.terminal_value_concentration:.1%}"
        for result in analysis.scenarios
        if result.terminal_value_concentration >= _HIGH_TERMINAL_VALUE_CONCENTRATION
    ]
    if concentrated:
        analysis.warnings.append(
            "terminal value concentration is high ("
            + ", ".join(concentrated)
            + "); small terminal assumptions can dominate the result"
        )

    if comparison_price is not None:
        implied_growth = implied_growth_rate(
            current_price=comparison_price,
            base_fcf=analysis.base_fcf,
            shares_outstanding=valuation_shares,
            discount_rate=discount_rate,
            terminal_growth_rate=terminal_growth_rate,
            years=years,
            annual_dilution_rate=annual_dilution_rate,
            terminal_dilution_rate=terminal_dilution_rate,
            lower=_REVERSE_GROWTH_BOUNDS[0],
            upper=_REVERSE_GROWTH_BOUNDS[1],
        )
        analysis.implied_five_year_growth = implied_growth
        analysis.implied_explicit_period_growth = implied_growth
        if implied_growth is None:
            analysis.warnings.append(
                "market price implies growth outside the reverse-DCF search range [-50%, 100%]"
            )

    analysis.available = True
    if snapshot.sector in (SectorClass.ENERGY, SectorClass.UTILITY):
        analysis.warnings.append(
            f"{snapshot.sector.value} cash flow is often cyclical or capital-cycle dependent; "
            "normalise the trailing base before relying on this sensitivity"
        )
    if annual_dilution_rate == 0.0 and effective_terminal_dilution == 0.0:
        analysis.warnings.append(
            "zero dilution does not capture the per-share cost of stock-based compensation or "
            "other future equity issuance"
        )
    analysis.warnings.append(
        "CFO minus capex is a levered proxy, not canonical FCFE because debt principal flows are "
        "excluded; scenario growth rates are illustrations, not analyst estimates or a "
        "recommendation"
    )
    return analysis
