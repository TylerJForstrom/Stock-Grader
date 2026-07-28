"""Aggregators: many weighted scores -> one score.

Used twice in the pipeline — metrics into a pillar, then pillars into the final grade — and the
choice of aggregator answers one question: **can a strength compensate for a weakness?**

An arithmetic mean says yes, completely: a company scoring 100 on valuation and 0 on solvency gets
a 50, the same as one scoring 50 on both. Those are not the same company. The second is mediocre;
the first is a cheap stock that may not survive. The power-mean family (:func:`ces`) makes that
trade-off an explicit dial rather than an accident of implementation:

===========  ===================================================
``rho``      behaviour
===========  ===================================================
``1``        arithmetic mean — full compensation
``0``        geometric mean — a zero anywhere zeroes the result
``-1``       harmonic mean — weak scores dominate
``-> -inf``  minimum — pure weakest-link
===========  ===================================================

Every aggregator here shares one contract: it takes scores and weights that may contain NaN, drops
the NaN entries, **renormalises the surviving weights to sum to 1**, and returns ``None`` if
nothing survives. Renormalisation is not a detail — without it, a missing metric silently drags a
pillar toward zero, and companies with sparse data would grade worse purely for being sparse.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .registry import AGGREGATORS

__all__ = [
    "aggregate",
    "align_and_renormalize",
    "ces",
    "geometric_mean",
    "harmonic_mean",
    "owa",
    "soft_min",
    "topsis_score",
    "trimmed_mean",
    "weighted_mean",
    "weighted_median",
]

_EPS = 1e-12


def align_and_renormalize(
    scores: pd.Series, weights: pd.Series | None = None
) -> tuple[pd.Series, pd.Series]:
    """Drop missing scores and renormalise the surviving weights to sum to 1.

    The single most important function in this module. A pillar of ten metrics where three could
    not be computed must be the weighted average of the seven that *were*, at full strength — not
    seven-tenths of a score. The coverage penalty for those three missing metrics is applied
    separately and visibly, in the model-sensitivity range, not smuggled in as a silent haircut.
    """
    scores = pd.Series(scores, dtype="float64")
    if weights is None:
        weights = pd.Series(1.0, index=scores.index, dtype="float64")
    else:
        weights = pd.Series(weights, dtype="float64").reindex(scores.index).fillna(0.0)

    mask = scores.notna() & weights.notna() & (weights > 0) & np.isfinite(scores.to_numpy())
    scores, weights = scores[mask], weights[mask]
    total = float(weights.sum())
    if scores.empty or total <= _EPS:
        return pd.Series(dtype="float64"), pd.Series(dtype="float64")
    return scores, weights / total


@AGGREGATORS("weighted_mean")
def weighted_mean(scores: pd.Series, weights: pd.Series | None = None, **_: object) -> float | None:
    """Weighted arithmetic mean. Full compensation between components."""
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    return float((s * w).sum())


@AGGREGATORS("weighted_median")
def weighted_median(scores: pd.Series, weights: pd.Series | None = None, **_: object) -> float | None:
    """Weighted median — ignores the magnitude of outliers entirely."""
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    order = s.sort_values().index
    cumulative = w.loc[order].cumsum()
    position = cumulative.searchsorted(0.5)
    position = min(int(position), len(order) - 1)
    return float(s.loc[order[position]])


@AGGREGATORS("trimmed_mean")
def trimmed_mean(
    scores: pd.Series, weights: pd.Series | None = None, *, trim: float = 0.1, **_: object
) -> float | None:
    """Discard the top and bottom ``trim`` fraction, then take the weighted mean."""
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    n = len(s)
    k = int(np.floor(n * trim))
    if k == 0 or n - 2 * k < 1:
        return float((s * w).sum())
    keep = s.sort_values().index[k : n - k]
    return weighted_mean(s.loc[keep], w.loc[keep])


@AGGREGATORS("geometric_mean")
def geometric_mean(scores: pd.Series, weights: pd.Series | None = None, **_: object) -> float | None:
    """Weighted geometric mean.

    Scores are shifted off zero by a small epsilon before taking logs: a literal zero would make
    the whole pillar zero, which is too brutal for a 0..100 scale where 0 means "worst in the
    universe" rather than "worthless".
    """
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    shifted = s.clip(lower=_EPS)
    return float(np.exp((w * np.log(shifted)).sum()))


@AGGREGATORS("harmonic_mean")
def harmonic_mean(scores: pd.Series, weights: pd.Series | None = None, **_: object) -> float | None:
    """Weighted harmonic mean. Punishes low scores hard."""
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    shifted = s.clip(lower=_EPS)
    denominator = float((w / shifted).sum())
    if denominator <= _EPS:
        return None
    return float(1.0 / denominator)


@AGGREGATORS("ces")
def ces(
    scores: pd.Series, weights: pd.Series | None = None, *, rho: float = 0.5, **_: object
) -> float | None:
    """Weighted power mean ``(sum w_i * x_i^rho)^(1/rho)`` — the compensation dial.

    ``rho = 1`` is the arithmetic mean, ``rho -> 0`` the geometric mean (handled as a limit),
    ``rho = -1`` the harmonic mean, ``rho -> -inf`` the minimum. Values in ``(0, 1)`` give partial
    compensation, which is the defensible default for a stock grade: a great balance sheet really
    does offset a mediocre valuation, but only partly.
    """
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    if abs(rho) < 1e-6:
        return geometric_mean(s, w)
    shifted = s.clip(lower=_EPS)
    try:
        powered = float((w * np.power(shifted, rho)).sum())
    except (OverflowError, FloatingPointError):
        return None
    if powered <= 0 or not np.isfinite(powered):
        return None
    result = powered ** (1.0 / rho)
    return float(result) if np.isfinite(result) else None


@AGGREGATORS("soft_min")
def soft_min(
    scores: pd.Series, weights: pd.Series | None = None, *, temperature: float = 10.0, **_: object
) -> float | None:
    """Smooth weakest-link via log-sum-exp.

    As ``temperature -> 0`` this becomes the hard minimum; at large temperature it approaches the
    weighted mean. Useful for a solvency pillar, where one genuinely alarming ratio should drag the
    pillar down regardless of how healthy everything else looks.
    """
    s, w = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    if temperature <= _EPS:
        return float(s.min())
    scaled = -s / temperature
    peak = float(scaled.max())
    total = float((w * np.exp(scaled - peak)).sum())
    if total <= 0:
        return float(s.min())
    return float(-temperature * (peak + np.log(total)))


@AGGREGATORS("owa")
def owa(
    scores: pd.Series, weights: pd.Series | None = None, *, pessimism: float = 0.5, **_: object
) -> float | None:
    """Ordered weighted averaging: weight by *rank*, not by identity.

    ``pessimism = 0`` weights the best scores, ``1`` the worst, ``0.5`` is the plain mean. This
    expresses "I care about a company's weakest three attributes, whichever they turn out to be" —
    something identity-based weights cannot say.
    """
    s, _ = align_and_renormalize(scores, weights)
    if s.empty:
        return None
    n = len(s)
    if n == 1:
        return float(s.iloc[0])
    ordered = s.sort_values(ascending=False).to_numpy()
    positions = np.arange(n, dtype="float64") / (n - 1)
    rank_weights = np.exp(-((positions - pessimism) ** 2) / 0.1)
    total = rank_weights.sum()
    if total <= _EPS:
        return float(s.mean())
    return float((ordered * rank_weights).sum() / total)


def topsis_score(matrix: pd.DataFrame, weights: pd.Series, **_: object) -> pd.Series:
    """TOPSIS: rank by closeness to the ideal and distance from the anti-ideal.

    An alternative to averaging altogether — the composite is a geometric relationship in
    attribute space rather than a weighted sum, so it rewards a balanced profile over one that
    averages the same way through extremes. Operates on a whole universe at once.
    """
    clean = matrix.astype("float64")
    norms = np.sqrt((clean**2).sum())
    norms = norms.replace(0.0, np.nan)
    normalized = clean / norms
    w = pd.Series(weights, dtype="float64").reindex(clean.columns).fillna(0.0)
    if w.sum() <= _EPS:
        w = pd.Series(1.0 / len(clean.columns), index=clean.columns)
    else:
        w = w / w.sum()
    weighted = normalized * w
    ideal, anti_ideal = weighted.max(), weighted.min()
    d_best = np.sqrt(((weighted - ideal) ** 2).sum(axis=1))
    d_worst = np.sqrt(((weighted - anti_ideal) ** 2).sum(axis=1))
    denominator = d_best + d_worst
    closeness = (d_worst / denominator.replace(0.0, np.nan)) * 100.0
    return closeness.astype("float64")


def aggregate(
    scores: pd.Series,
    weights: pd.Series | None = None,
    *,
    method: str = "ces",
    **kwargs: object,
) -> float | None:
    """Aggregate by registered method name."""
    return AGGREGATORS.get(method)(scores, weights, **kwargs)
