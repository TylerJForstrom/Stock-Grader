"""Score -> letter grade, model-sensitivity range, and contribution decomposition.

Three concerns that all belong to the last mile of the pipeline:

**Grade scale.** A 0..100 composite becomes A+ .. F under one of three curve modes.
``cross_sectional`` uses a peer percentile and is the production default. The legacy
``absolute`` name applies fixed letter cutoffs to a score that was still normalized from the peer
cross-section; ``hybrid`` blends that standardized composite with its percentile. Neither legacy
mode is an intrinsic value or a peer-independent quality score.

**Model sensitivity.** A grade computed from 12 of 40 metrics is not the same claim as one computed
from all 40, and a grade that flips from B to D when the weights are perturbed slightly is not a
"B". The resampling below quantifies sensitivity to modelling choices; it is not a statistical
confidence interval for investment value or future returns.

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
    "PERCENTILE_CUTOFFS",
    "apply_hysteresis",
    "coverage_penalty",
    "explain_aggregate_contributions",
    "explain_contributions",
    "grade_from_percentile",
    "hazen_percentile",
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

def hazen_percentile(value: float | None, other_values: pd.Series | np.ndarray | list[float]) -> float | None:
    """Tie-aware Hazen percentile after inserting ``value`` among ``other_values``.

    The target observation is deliberately *not* expected in ``other_values``.  Point grading
    removes the target's current composite and inserts it once; sensitivity draws replace that
    point with the draw and insert it once.  Using this same operation in both places prevents the
    old contradiction where ``side="right"`` awarded every tied point the top of its tie while
    ``side="left"`` assigned the same value the bottom of the tie in its interval.

    Hazen plotting positions are ``(average_rank - 0.5) / n``.  They are centred at 50 for a
    symmetric cross-section and never assert that a finite sample has observed the literal 0th or
    100th population percentile.
    """
    if value is None or not np.isfinite(value):
        return None
    peers = np.asarray(other_values, dtype="float64").reshape(-1)
    peers = peers[np.isfinite(peers)]
    target = float(value)
    n = peers.size + 1
    below = int(np.count_nonzero(peers < target))
    tied = int(np.count_nonzero(peers == target)) + 1  # include the inserted target
    average_rank = below + (tied + 1.0) / 2.0  # one-based midrank
    return float((average_rank - 0.5) / n * 100.0)


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
    """Blend a peer-standardized composite with its percentile-implied score.

    Both inputs are peer-derived. This compatibility curve smooths ordinal rank jumps but must not
    be described as an absolute-quality or intrinsic-value safeguard.
    """
    if score is None or not np.isfinite(score):
        if percentile is None:
            return (float("nan"), "N/A")
        return (float(percentile), grade_from_percentile(percentile))
    if percentile is None or not np.isfinite(percentile):
        return (float(score), to_letter(score))
    blended = absolute_weight * float(score) + (1.0 - absolute_weight) * float(percentile)
    return (blended, to_letter(blended))


def apply_hysteresis(
    score: float,
    previous_letter: str | None,
    *,
    band: float = 1.0,
    cutoffs: list[tuple[float, str]] | None = None,
) -> str:
    """Keep a letter stable when the score moves only trivially.

    Without this, a score drifting between 70.9 and 71.1 flips B+/B on every refresh, which reads
    as a signal and is not one. A previously-assigned letter is retained while the score stays
    within ``band`` points of its boundary.
    """
    active_cutoffs = cutoffs or GRADE_CUTOFFS
    letter = to_letter(score, active_cutoffs)
    letters = [name for _, name in active_cutoffs]
    if previous_letter is None or previous_letter == letter or previous_letter not in letters:
        return letter
    if not np.isfinite(score) or not np.isfinite(band) or band < 0:
        return letter
    for index, (lower, name) in enumerate(active_cutoffs):
        if name != previous_letter:
            continue
        upper = 100.0 if index == 0 else active_cutoffs[index - 1][0]
        crossed_lower = score < lower and lower - score <= band
        crossed_upper = score >= upper and score - upper <= band
        if crossed_lower or crossed_upper:
            return previous_letter
        break
    return letter


def coverage_penalty(coverage: float, *, floor: float = 0.4) -> float:
    """Multiplier widening the model-sensitivity range as data coverage falls.

    At full coverage the multiplier is 1. As coverage drops the range widens; below ``floor`` the
    caller should decline to issue a grade at all rather than publish a number with a range so wide
    it carries no information.
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
    """Model-sensitivity range for a composite score, from two perturbations.

    1. **Weight uncertainty.** The weight vector is one defensible choice among many, so it is
       resampled from a Dirichlet distribution centred on it. Lower ``concentration`` means wider
       perturbations around the configured weights.
    2. **Metric-set uncertainty.** A random ``leave_out`` fraction of metrics is dropped on each
       draw, which asks whether the grade depends on a couple of lucky inputs.

    The range is the 5th to 95th percentile of the resulting scenario distribution, widened by the
    coverage penalty.  It describes sensitivity to these explicit perturbations only.  It does not
    cover filing error, peer-universe choice, normalization estimation, model misspecification, or
    future returns, and therefore must not be interpreted as a statistical confidence interval.
    """
    scores, w = align_and_renormalize(metric_scores, weights)
    if scores.empty:
        return np.array([]) if return_samples else (float("nan"), float("nan"))
    if len(scores) == 1:
        centre = float(scores.iloc[0])
        half = 5.0 * coverage_penalty(coverage)
        if return_samples:
            # A single surviving component has no weight/set variation to resample.  Emit an
            # explicit symmetric sensitivity spread rather than a zero-width range that falsely
            # claims perfect stability at arbitrarily poor coverage.
            if draws <= 1:
                return np.array([centre], dtype="float64")
            return np.linspace(
                max(0.0, centre - half),
                min(100.0, centre + half),
                draws,
                dtype="float64",
            )
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


def explain_aggregate_contributions(
    component_scores: pd.Series,
    weights: pd.Series,
    *,
    aggregator: str = "weighted_mean",
    neutral: float = 50.0,
    max_exact_components: int = 12,
    **aggregator_kwargs: object,
) -> dict[str, float]:
    """Baseline attribution for a pointwise aggregate that exactly reconciles.

    Arithmetic ``weight * (score - 50)`` only reconstructs an arithmetic mean.  The shipped
    profile composite is normally CES, so using that arithmetic explanation could claim a
    different score from the one actually reported. This function treats a neutral component as
    the baseline. For differentiable power means it uses an Aumann-Shapley path integral; for
    other aggregators it uses exact baseline Shapley values. The resulting values sum to
    ``aggregate(actual) - aggregate(all-neutral)`` (normally ``composite - 50``).

    Pillar composites contain at most ten components, making exact enumeration cheap.  The
    ``max_exact_components`` guard prevents an accidental exponential call on a large metric
    catalogue; weighted arithmetic remains available analytically at any size.
    """
    scores, effective = align_and_renormalize(component_scores, weights)
    if scores.empty:
        return {}
    if aggregator == "weighted_mean":
        return {
            name: float(effective[name] * (scores[name] - neutral))
            for name in scores.index
        }

    names = list(scores.index)
    n = len(names)
    if aggregator in {"ces", "geometric_mean", "harmonic_mean"}:
        if aggregator == "geometric_mean":
            rho = 0.0
        elif aggregator == "harmonic_mean":
            rho = -1.0
        else:
            rho = float(aggregator_kwargs.get("rho", 0.5))
        actual = scores.to_numpy(dtype="float64")
        effective_array = effective.to_numpy(dtype="float64")
        deltas = actual - float(neutral)
        # Gauss-Legendre integration is deterministic and evaluates all component gradients in
        # one vector operation per node. This makes a ten-pillar explanation milliseconds rather
        # than the thousands of aggregate calls required by subset enumeration for every stock.
        nodes, quadrature_weights = np.polynomial.legendre.leggauss(32)
        path_positions = (nodes + 1.0) / 2.0
        quadrature_weights = quadrature_weights / 2.0
        integrated_gradient = np.zeros(n, dtype="float64")
        for position, quadrature_weight in zip(
            path_positions, quadrature_weights, strict=True
        ):
            path = np.clip(float(neutral) + position * deltas, _EPS, None)
            if abs(rho) < 1e-8:
                aggregate_value = float(
                    np.exp(np.sum(effective_array * np.log(path)))
                )
                gradient = aggregate_value * effective_array / path
            else:
                powered_sum = float(np.sum(effective_array * np.power(path, rho)))
                aggregate_value = powered_sum ** (1.0 / rho)
                gradient = (
                    effective_array
                    * np.power(path, rho - 1.0)
                    * aggregate_value ** (1.0 - rho)
                )
            integrated_gradient += quadrature_weight * gradient
        values = deltas * integrated_gradient
        result = {name: float(values[index]) for index, name in enumerate(names)}
        full = aggregate(
            scores,
            effective,
            method=aggregator,
            **aggregator_kwargs,
        )
        baseline = aggregate(
            pd.Series(float(neutral), index=names, dtype="float64"),
            effective,
            method=aggregator,
            **aggregator_kwargs,
        )
        if full is not None and baseline is not None and result:
            residual = float(full - baseline - sum(result.values()))
            anchor = max(result, key=lambda key: abs(result[key]))
            result[anchor] += residual
        return result

    if n > max_exact_components:
        raise ValueError(
            f"exact contribution attribution supports at most {max_exact_components} components; "
            f"got {n} for {aggregator!r}"
        )

    from math import factorial

    actual = scores.to_numpy(dtype="float64")
    neutral_values = np.full(n, float(neutral), dtype="float64")
    effective_array = effective.to_numpy(dtype="float64")
    cache: dict[int, float] = {}

    # CES is the production default and this attribution runs once per security.  Reconstructing
    # two pandas objects and redispatching through the registry for every one of its 2**n
    # coalitions made an ordinary twelve-name grade take tens of seconds.  The direct expression
    # below is algebraically identical because all coalition components remain present (excluded
    # components take the neutral value), so the already-normalised weights do not change.
    ces_rho: float | None = None
    ces_baseline = 0.0
    ces_increments: np.ndarray | None = None
    if aggregator == "ces":
        ces_rho = float(aggregator_kwargs.get("rho", 0.5))
        shifted_actual = np.clip(actual, 1e-12, None)
        shifted_neutral = np.clip(neutral_values, 1e-12, None)
        if abs(ces_rho) < 1e-6:
            neutral_terms = effective_array * np.log(shifted_neutral)
            actual_terms = effective_array * np.log(shifted_actual)
        else:
            neutral_terms = effective_array * np.power(shifted_neutral, ces_rho)
            actual_terms = effective_array * np.power(shifted_actual, ces_rho)
        ces_baseline = float(neutral_terms.sum())
        ces_increments = actual_terms - neutral_terms

    def coalition_value(mask: int) -> float:
        cached = cache.get(mask)
        if cached is not None:
            return cached
        if ces_rho is not None and ces_increments is not None:
            total = ces_baseline
            for i in range(n):
                if mask & (1 << i):
                    total += float(ces_increments[i])
            if abs(ces_rho) < 1e-6:
                value = float(np.exp(total))
            else:
                value = float(total ** (1.0 / ces_rho))
            cache[mask] = value
            return value
        values = neutral_values.copy()
        for i in range(n):
            if mask & (1 << i):
                values[i] = actual[i]
        result = aggregate(
            pd.Series(values, index=names, dtype="float64"),
            pd.Series(effective_array, index=names, dtype="float64"),
            method=aggregator,
            **aggregator_kwargs,
        )
        value = float("nan") if result is None else float(result)
        cache[mask] = value
        return value

    normalizer = factorial(n)
    contributions = dict.fromkeys(names, 0.0)
    full_mask = (1 << n) - 1
    for i, name in enumerate(names):
        bit = 1 << i
        for mask in range(full_mask + 1):
            if mask & bit:
                continue
            size = mask.bit_count()
            coefficient = factorial(size) * factorial(n - size - 1) / normalizer
            without = coalition_value(mask)
            with_component = coalition_value(mask | bit)
            if np.isfinite(without) and np.isfinite(with_component):
                contributions[name] += coefficient * (with_component - without)

    # Floating summation across many coalitions can leave a few ulps of residual.  Assign it to the
    # largest attribution so the public audit invariant is exact without changing the ordering.
    full_value = coalition_value(full_mask)
    baseline_value = coalition_value(0)
    residual = (full_value - baseline_value) - sum(contributions.values())
    if contributions and np.isfinite(residual):
        anchor = max(contributions, key=lambda key: abs(contributions[key]))
        contributions[anchor] += residual
    return {name: float(value) for name, value in contributions.items()}


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
        contributions=explain_aggregate_contributions(
            metric_scores,
            weights,
            aggregator=aggregator,
            **aggregator_kwargs,
        ),
        metric_scores={k: float(v) for k, v in scores.items()},
        coverage=(ok / applicable) if applicable else 0.0,
        n_metrics=ok,
        n_missing=missing,
        n_not_applicable=not_applicable,
        weighting_method=weighting_method,
        aggregator=aggregator,
        warnings=list(warnings or []),
    )
