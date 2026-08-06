"""Guarded arithmetic for metric authors.

Every metric in the catalogue is built from these helpers rather than raw operators, because the
single largest source of silently-wrong stock grades is unguarded division:

* A company with negative earnings gets a **negative** P/E. Sorted ascending, "cheapest first", it
  lands at the very top of the value screen. The correct answer is that P/E is *undefined* for a
  loss-maker, so :func:`safe_div` refuses non-positive denominators when asked to.
* A company with negative book value gets a negative P/B, same trap.
* A CAGR computed from a negative starting value returns a complex number or a nonsense sign.
* An interest-coverage ratio with zero interest expense is ``inf``, which poisons every mean,
  z-score and correlation it touches downstream.

The rule throughout: **return ``None``, never a sentinel and never a zero.** ``None`` propagates
into ``Coverage.MISSING``, the weight is renormalised away, and the report says so. A zero would be
silently averaged in as a real observation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypeGuard

import numpy as np
import pandas as pd

# A noiseless fit implies an infinite t-statistic; report it as merely enormous so downstream
# aggregation stays in the reals.
_T_STAT_CAP = 1e6

__all__ = [
    "cagr",
    "clip_finite",
    "coalesce",
    "consistency",
    "growth_rate",
    "is_finite_number",
    "linear_trend",
    "mean_or_none",
    "r_squared_loglinear",
    "safe_div",
    "safe_log",
    "winsorize",
]


def is_finite_number(value: object) -> TypeGuard[int | float | np.integer | np.floating]:
    """True for a real, finite number. NaN, inf and None are all rejected.

    Declared as a TypeGuard so every ``float(value)`` guarded by this function
    type-checks on the narrowed type instead of on bare ``object`` — the check
    below is exactly the isinstance test the guard reports.
    """
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float, np.integer, np.floating)):
        return False
    return math.isfinite(float(value))


def coalesce(*values: object) -> float | None:
    """First finite value, else ``None``. For concepts with several possible sources."""
    for value in values:
        if is_finite_number(value):
            return float(value)
    return None


def safe_div(
    numerator: object,
    denominator: object,
    *,
    positive_denominator: bool = False,
    cap: float | None = None,
) -> float | None:
    """Divide, returning ``None`` for any degenerate case.

    Args:
        positive_denominator: when True, a zero **or negative** denominator yields ``None``. Set
            this for every valuation multiple. A loss-making company does not have a low P/E, it
            has no P/E, and the difference decides whether it tops or is excluded from a value
            screen.
        cap: optional absolute clamp on the result. A P/E of 40,000 from a company that earned one
            cent is arithmetically true and analytically meaningless; clamping keeps it from
            dominating a cross-sectional z-score.
    """
    if not is_finite_number(numerator) or not is_finite_number(denominator):
        return None
    num, den = float(numerator), float(denominator)
    if den == 0.0:
        return None
    if positive_denominator and den <= 0.0:
        return None
    result = num / den
    if not math.isfinite(result):
        return None
    if cap is not None:
        result = max(-abs(cap), min(abs(cap), result))
    return result


def safe_log(value: object) -> float | None:
    """Natural log, ``None`` for non-positive or non-finite input."""
    if not is_finite_number(value) or float(value) <= 0.0:
        return None
    return math.log(float(value))


def cagr(begin: object, end: object, years: float) -> float | None:
    """Compound annual growth rate.

    Returns ``None`` when the starting value is non-positive. A CAGR measured from a negative or
    zero base is not a growth rate — raising a negative ratio to a fractional power is undefined in
    the reals, and the sign convention people reach for instead ("it improved from -10 to +5, call
    it +150%") is not comparable with any other company's growth rate. Loss-to-profit transitions
    are real and important, but they belong in a dedicated turnaround metric, not smuggled into a
    CAGR that will be z-scored against ordinary compounding.
    """
    if not is_finite_number(begin) or not is_finite_number(end):
        return None
    begin_f, end_f = float(begin), float(end)
    if begin_f <= 0.0 or years <= 0.0:
        return None
    ratio = end_f / begin_f
    if ratio <= 0.0:
        return None
    try:
        return ratio ** (1.0 / years) - 1.0
    except (ValueError, OverflowError):
        return None


def growth_rate(begin: object, end: object) -> float | None:
    """Simple period-over-period growth, guarded against a non-positive base."""
    if not is_finite_number(begin) or not is_finite_number(end):
        return None
    begin_f = float(begin)
    if begin_f <= 0.0:
        return None
    return (float(end) - begin_f) / begin_f


def mean_or_none(values: Sequence[object]) -> float | None:
    """Mean of the finite entries, or ``None`` if there are none."""
    finite = [float(v) for v in values if is_finite_number(v)]
    return sum(finite) / len(finite) if finite else None


def winsorize(series: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """Clamp a cross-section to its own quantiles.

    Applied before z-scoring: a single extreme observation can otherwise consume most of the
    standard deviation and compress every other name toward zero.
    """
    clean = series.dropna()
    if clean.empty:
        return series
    lo, hi = clean.quantile(lower), clean.quantile(upper)
    return series.clip(lower=lo, upper=hi)


def clip_finite(series: pd.Series) -> pd.Series:
    """Replace inf/-inf with NaN so they are treated as missing rather than as extremes."""
    return series.replace([np.inf, -np.inf], np.nan)


def linear_trend(series: pd.Series | Sequence[float]) -> tuple[float, float] | None:
    """OLS slope and t-statistic of a series against its own index position.

    Used for margin trend, revenue-trend and regression-channel metrics. Returns ``None`` when
    fewer than three finite points are available — a two-point "trend" has no residual degrees of
    freedom and its t-statistic is undefined.
    """
    values = pd.Series(list(series), dtype="float64").dropna()
    n = len(values)
    if n < 3:
        return None
    x = np.arange(n, dtype="float64")
    y = values.to_numpy()
    if not np.all(np.isfinite(y)):
        # An inf in the series (a zero-revenue year in a margin history) makes the slope NaN, and
        # copysign(CAP, nan) returned +1,000,000 — a maximally POSITIVE trend, so the worst input
        # in the universe scored as the best improving margin.
        return None
    x_centered = x - x.mean()
    denom = float((x_centered**2).sum())
    if denom == 0.0:
        return None
    slope = float((x_centered * (y - y.mean())).sum() / denom)
    if not math.isfinite(slope):
        return None
    intercept = float(y.mean() - slope * x.mean())
    residuals = y - (intercept + slope * x)
    dof = n - 2
    sse = float((residuals**2).sum())
    # A perfect fit has zero residual variance and an infinite t-statistic. Returning inf would
    # propagate into every downstream mean, z-score and correlation, so it is capped: a noiseless
    # trend is reported as overwhelmingly significant rather than as a number arithmetic cannot use.
    if dof <= 0 or sse <= 0.0:
        return (slope, math.copysign(_T_STAT_CAP, slope) if slope != 0 else 0.0)
    se = math.sqrt(sse / dof / denom)
    if se == 0.0:
        return (slope, math.copysign(_T_STAT_CAP, slope) if slope != 0 else 0.0)
    t_stat = slope / se
    if not math.isfinite(t_stat):
        return (slope, math.copysign(_T_STAT_CAP, slope) if slope != 0 else 0.0)
    return (slope, max(-_T_STAT_CAP, min(_T_STAT_CAP, t_stat)))


def r_squared_loglinear(series: pd.Series | Sequence[float]) -> float | None:
    """R-squared of a log-linear fit — how *steadily* a quantity compounds.

    This is the growth-consistency measure: two companies can share a 15% revenue CAGR while one
    grew 15% every year and the other collapsed then rebounded. Requires all-positive values, since
    the fit is in logs.
    """
    values = pd.Series(list(series), dtype="float64").dropna()
    if len(values) < 3 or (values <= 0).any():
        return None
    y = np.log(values.to_numpy())
    x = np.arange(len(y), dtype="float64")
    x_centered = x - x.mean()
    denom = float((x_centered**2).sum())
    if denom == 0.0:
        return None
    slope = float((x_centered * (y - y.mean())).sum() / denom)
    intercept = float(y.mean() - slope * x.mean())
    predicted = intercept + slope * x
    ss_res = float(((y - predicted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot <= 0.0:
        return None
    return 1.0 - ss_res / ss_tot


def consistency(values: Sequence[object]) -> float | None:
    """Fraction of periods with a positive change — a distribution-free steadiness measure."""
    finite = [float(v) for v in values if is_finite_number(v)]
    if len(finite) < 2:
        return None
    diffs = np.diff(finite)
    return float((diffs > 0).mean())
