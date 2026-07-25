"""Score -> letter grade, confidence interval, and contribution decomposition.

Three concerns that all belong to the last mile of the pipeline:

**Grade scale.** A 0..100 composite becomes A+ .. F under one of three curve modes. ``absolute``
uses fixed cutoffs, so a bad company grades badly even in a bad universe. ``cross_sectional`` uses
percentile of peers, so the grade always means "relative to this universe". ``hybrid`` blends them,
which is usually what a person actually wants: mostly relative, but a universe of uniformly awful
companies should not manufacture an A.

**Uncertainty.** A grade computed from 12 of 40 metrics is not the same claim as one computed from
all 40, and a grade that flips from B to D when the weights are perturbed slightly is not a
"B". Both are quantified rather than hidden.

**Explanation.** Every grade point is traceable to a metric, its score, and its effective weight,
so any grade can be argued with.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .aggregate import aggregate, align_and_renormalize
from .types import Coverage, MetricResult, PillarScore

__all__ = [
    "GRADE_CUTOFFS",
    "apply_hysteresis",
    "coverage_penalty",
    "explain_contributions",
    "grade_from_percentile",
    "hybrid_grade",
    "letter_to_midpoint",
    "to_letter",
    "uncertainty_interval",
]


# Absolute cutoffs: the minimum composite score for each letter. Deliberately not a school curve —
# 50 is a genuinely average company and grades C, which is the honest answer for most stocks.
GRADE_CUTOFFS: list[tuple[float, str]] = [
    (90.0, "A+"),
    (83.0, "A"),
    (77.0, "A-"),
    (71.0, "B+"),
    (65.0, "B"),
    (59.0, "B-"),
    (53.0, "C+"),
    (47.0, "C"),
    (41.0, "C-"),
    (35.0, "D+"),
    (29.0, "D"),
    (23.0, "D-"),
    (0.0, "F"),
]

# Percentile thresholds for cross-sectional grading. Roughly a normal curve: ~7% get an A or
# better, ~7% get a D- or worse.
PERCENTILE_CUTOFFS: list[tuple[float, str]] = [
    (97.0, "A+"),
    (93.0, "A"),
    (88.0, "A-"),
    (80.0, "B+"),
    (70.0, "B"),
    (60.0, "B-"),
    (50.0, "C+"),
    (40.0, "C"),
    (30.0, "C-"),
    (20.0, "D+"),
    (12.0, "D"),
    (6.0, "D-"),
    (0.0, "F"),
]

_LETTER_ORDER = [letter for _, letter in GRADE_CUTOFFS]


def to_letter(score: float | None, cutoffs: list[tuple[float, str]] | None = None) -> str:
    """Map a 0..100 score to a letter. ``None`` yields ``"N/A"`` — an ungraded stock, not an F."""
    if score is None or not np.isfinite(score):
        return "N/A"
    for threshold, letter in cutoffs or GRADE_CUTOFFS:
        if score >= threshold:
            return letter
    return "F"


def letter_to_midpoint(letter: str) -> float | None:
    """Approximate numeric centre of a letter band, for round-tripping and comparison."""
    cutoffs = GRADE_CUTOFFS
    for i, (threshold, name) in enumerate(cutoffs):
        if name == letter:
            upper = 100.0 if i == 0 else cutoffs[i - 1][0]
            return (threshold + upper) / 2.0
    return None


def grade_from_percentile(percentile: float | None) -> str:
    """Letter from a universe percentile (0..100)."""
    if percentile is None or not np.isfinite(percentile):
        return "N/A"
    for threshold, letter in PERCENTILE_CUTOFFS:
        if percentile >= threshold:
            return letter
    return "F"


def hybrid_grade(
    score: float | None,
    percentile: float | None,
    *,
    absolute_weight: float = 0.5,
) -> tuple[float, str]:
    """Blend the absolute score with the percentile-implied score, then grade the blend.

    Guards against both failure modes of a single mode: a strong company in a strong universe is
    not dragged to a C by its middling rank, and a weak company in a weak universe does not get an
    A for being the least bad.
    """
    if score is None or not np.isfinite(score):
        if percentile is None:
            return (float("nan"), "N/A")
        return (float(percentile), grade_from_percentile(percentile))
    if percentile is None or not np.isfinite(percentile):
        return (float(score), to_letter(score))
    blended = absolute_weight * float(score) + (1.0 - absolute_weight) * float(percentile)
    return (blended, to_letter(blended))


def apply_hysteresis(score: float, previous_letter: str | None, *, band: float = 1.0) -> str:
    """Keep a letter stable when the score moves only trivially.

    Without this, a score drifting between 70.9 and 71.1 flips B+/B on every refresh, which reads
    as a signal and is not one. A previously-assigned letter is retained while the score stays
    within ``band`` points of its boundary.
    """
    letter = to_letter(score)
    if previous_letter is None or previous_letter == letter or previous_letter not in _LETTER_ORDER:
        return letter
    for threshold, name in GRADE_CUTOFFS:
        if name == previous_letter and abs(score - threshold) <= band:
            return previous_letter
    return letter


def coverage_penalty(coverage: float, *, floor: float = 0.4) -> float:
    """Multiplier widening the confidence interval as data coverage falls.

    At full coverage the multiplier is 1. As coverage drops the interval widens; below ``floor``
    the caller should decline to issue a grade at all rather than publish a number with an interval
    so wide it carries no information.
    """
    coverage = float(np.clip(coverage, 0.0, 1.0))
    if coverage >= 1.0:
        return 1.0
    return float(1.0 + 2.0 * (1.0 - coverage) ** 2)


def uncertainty_interval(
    metric_scores: pd.Series,
    weights: pd.Series,
    *,
    coverage: float = 1.0,
    aggregator: str = "weighted_mean",
    draws: int = 500,
    concentration: float = 50.0,
    leave_out: float = 0.15,
    seed: int = 0,
    return_samples: bool = False,
    **aggregator_kwargs: object,
) -> tuple[float, float] | np.ndarray:
    """Confidence interval for a composite score, from two sources of doubt.

    1. **Weight uncertainty.** The weight vector is one defensible choice among many, so it is
       resampled from a Dirichlet distribution centred on it. Lower ``concentration`` means less
       confidence in the exact weights.
    2. **Metric-set uncertainty.** A random ``leave_out`` fraction of metrics is dropped on each
       draw, which asks whether the grade depends on a couple of lucky inputs.

    The interval is the 5th to 95th percentile of the resulting distribution, widened by the
    coverage penalty. A wide interval is a real finding: it says this company cannot be graded
    confidently, which is more useful than a precise-looking number that is not.
    """
    scores, w = align_and_renormalize(metric_scores, weights)
    if scores.empty:
        return np.array([]) if return_samples else (float("nan"), float("nan"))
    if len(scores) == 1:
        centre = float(scores.iloc[0])
        half = 5.0 * coverage_penalty(coverage)
        if return_samples:
            return np.full(draws, centre)
        return (max(0.0, centre - half), min(100.0, centre + half))

    rng = np.random.default_rng(seed)
    alpha = np.clip(w.to_numpy() * concentration, 1e-3, None)
    n = len(scores)
    keep_n = max(2, int(round(n * (1.0 - leave_out))))
    values = scores.to_numpy()

    # All draws at once. Every aggregator in the power-mean family is a closed-form expression over
    # the (draws x n) weight matrix, so the whole resampling is a couple of array operations rather
    # than a few hundred DataFrame round-trips — the difference between a fast grade and one that
    # takes minutes over a 500-name universe.
    weights_matrix = rng.dirichlet(alpha, size=draws)
    if keep_n < n:
        mask = np.zeros((draws, n), dtype=bool)
        for row in range(draws):
            mask[row, rng.choice(n, size=keep_n, replace=False)] = True
        weights_matrix = np.where(mask, weights_matrix, 0.0)
    totals = weights_matrix.sum(axis=1, keepdims=True)
    weights_matrix = np.divide(
        weights_matrix, totals, out=np.zeros_like(weights_matrix), where=totals > _EPS
    )

    samples = _vectorised_power_mean(values, weights_matrix, aggregator, aggregator_kwargs)
    if samples is None:
        samples = np.empty(draws, dtype="float64")
        for i in range(draws):
            row = weights_matrix[i]
            active = row > 0
            result = aggregate(
                pd.Series(values[active], index=scores.index[active]),
                pd.Series(row[active], index=scores.index[active]),
                method=aggregator,
                **aggregator_kwargs,
            )
            samples[i] = np.nan if result is None else result

    finite = samples[np.isfinite(samples)]
    if return_samples:
        # The caller re-ranks each draw against the peer set before taking percentiles, which is
        # the only way the percentile half of a hybrid score carries any width at all.
        penalty = coverage_penalty(coverage)
        if finite.size and penalty > 1.0:
            centre = float(np.median(finite))
            finite = centre + (finite - centre) * penalty
        return finite
    if finite.size == 0:
        return (float("nan"), float("nan"))

    low, high = np.percentile(finite, [5.0, 95.0])
    centre = float(np.median(finite))
    penalty = coverage_penalty(coverage)
    low = centre - (centre - low) * penalty
    high = centre + (high - centre) * penalty
    return (float(np.clip(low, 0.0, 100.0)), float(np.clip(high, 0.0, 100.0)))


_EPS = 1e-12


def _vectorised_power_mean(
    values: np.ndarray,
    weights: np.ndarray,
    aggregator: str,
    kwargs: dict,
) -> np.ndarray | None:
    """Closed-form evaluation of the power-mean aggregators over a matrix of weight draws.

    Returns ``None`` for aggregators that are not power means (medians, order statistics, TOPSIS),
    which fall back to the per-draw loop.
    """
    rho: float | None
    if aggregator == "weighted_mean":
        rho = 1.0
    elif aggregator == "geometric_mean":
        rho = 0.0
    elif aggregator == "harmonic_mean":
        rho = -1.0
    elif aggregator == "ces":
        rho = float(kwargs.get("rho", 0.5))
    else:
        return None

    safe = np.clip(values, _EPS, None)
    if abs(rho) < 1e-6:
        return np.exp(weights @ np.log(safe))
    if rho == 1.0:
        return weights @ values
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        powered = weights @ np.power(safe, rho)
        result = np.power(np.clip(powered, _EPS, None), 1.0 / rho)
    return np.where(np.isfinite(result), result, np.nan)


def explain_contributions(
    metric_scores: pd.Series,
    weights: pd.Series,
    *,
    neutral: float = 50.0,
) -> dict[str, float]:
    """Decompose a pillar score into per-metric contributions **relative to neutral**.

    Reporting ``weight * score`` would make every metric look positive, since scores are all
    non-negative. Measuring against a neutral 50 instead answers the question a user actually has:
    which attributes pushed this grade up, and which dragged it down.
    """
    scores, w = align_and_renormalize(metric_scores, weights)
    if scores.empty:
        return {}
    return {name: float(w[name] * (scores[name] - neutral)) for name in scores.index}


def build_pillar_score(
    pillar: str,
    results: dict[str, MetricResult],
    metric_scores: pd.Series,
    weights: pd.Series,
    *,
    aggregator: str = "ces",
    weighting_method: str = "equal",
    warnings: list[str] | None = None,
    **aggregator_kwargs: object,
) -> PillarScore:
    """Assemble a :class:`~stock_grader.types.PillarScore` with coverage accounting.

    Coverage counts only *applicable* metrics, so a bank is not marked down for the manufacturing
    ratios that were never defined for it.
    """
    missing = sum(1 for r in results.values() if r.coverage is Coverage.MISSING)
    not_applicable = sum(1 for r in results.values() if r.coverage is Coverage.NOT_APPLICABLE)
    ok = sum(1 for r in results.values() if r.coverage is Coverage.OK)
    applicable = ok + missing

    score = aggregate(metric_scores, weights, method=aggregator, **aggregator_kwargs)
    scores, effective = align_and_renormalize(metric_scores, weights)

    return PillarScore(
        pillar=pillar,
        score=float(score) if score is not None else float("nan"),
        weights={k: float(v) for k, v in effective.items()},
        contributions=explain_contributions(metric_scores, weights),
        metric_scores={k: float(v) for k, v in scores.items()},
        coverage=(ok / applicable) if applicable else 0.0,
        n_metrics=ok,
        n_missing=missing,
        n_not_applicable=not_applicable,
        weighting_method=weighting_method,
        aggregator=aggregator,
        warnings=list(warnings or []),
    )
