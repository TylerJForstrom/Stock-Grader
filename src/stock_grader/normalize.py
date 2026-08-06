"""Normalizers: raw metric values -> comparable 0..100 scores.

Two regimes, and the difference matters:

**Cross-sectional** (``zscore``, ``percentile``, ``gaussian_rank``, ...) scores a name against a
peer universe. This is how factor investing actually works — "a 22x P/E" means nothing until you
know what the peers trade at — but it needs a universe, and it makes the grade *relative*: in a
universe of only bad companies, someone still scores 100.

**Absolute** (``piecewise``) maps a raw value through calibrated anchor points, so a single stock
can be graded with no universe at all and a genuinely bad company scores badly even among peers
that are worse. This is the fallback whenever ``n == 1``.

Direction handling is applied once, here, by flipping the score for ``direction == -1`` metrics.
Metrics must not pre-negate their own values — doing it in two places is how sign bugs happen.

``direction == 0`` marks a **non-monotonic** metric, where both extremes are bad and there is an
ideal band. A payout ratio of 0% and one of 95% are both concerning; a naive monotonic mapping
would rank the 95% payer as the best dividend stock in the universe.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from .registry import NORMALIZERS

__all__ = [
    "NEUTRAL_SCORE",
    "apply_direction",
    "double_sigmoid",
    "gaussian_rank",
    "minmax",
    "normalize_series",
    "percentile_rank",
    "piecewise",
    "quantile_bucket",
    "robust_z",
    "sector_neutral",
    "sigmoid_z",
    "winsorized_z",
    "zscore",
]

NEUTRAL_SCORE = 50.0
_MAD_CONSISTENCY = 1.4826  # makes MAD a consistent estimator of sigma under normality
_Z_CLIP = 3.0  # z-scores beyond +/-3 sigma map to the ends of the 0..100 scale
_WINSOR_SMALL_N = 100  # below this, quantile winsorisation is a no-op and a MAD fence is used


def _neutral(values: pd.Series) -> pd.Series:
    """NaN everywhere: a cross-section too degenerate to rank carries no information.

    This used to return 50 wherever the input existed, which is a *fabricated* score rather than a
    missing one. It matters most for the lone-financial case: put one bank in a universe of
    industrials and its bank-only metrics have a cross-section of exactly one member, so every one
    of them scored a full-weight neutral 50 and the bank was effectively graded blind on the very
    metrics added to grade it properly.

    NaN instead, so the aggregation layer renormalises the weight away and the coverage figure
    reports the shortfall. A metric with one observation cannot be ranked against anything.
    """
    return pd.Series(np.nan, index=values.index, dtype="float64")


def _to_score(z: pd.Series, clip: float = _Z_CLIP) -> pd.Series:
    """Map a z-score to 0..100, linearly, saturating at +/- ``clip`` sigma."""
    return ((z.clip(-clip, clip) + clip) / (2 * clip) * 100.0).astype("float64")


def _adaptive_clip(z: pd.Series) -> float:
    """Clip bound chosen from the cross-section rather than fixed at three MADs.

    A fixed fence saturates far more of a real universe than intuition suggests, because financial
    cross-sections are extraordinarily heavy-tailed — return on equity has an excess kurtosis of
    137 in the SEC's top-200-by-assets cross-section alone, so three MADs is nowhere near rare.
    Measured fractions landing at *exactly* 0 or *exactly* 100 under the fixed fence: ROE 17.3%,
    R&D intensity 15.0%, asset turnover 10.7%, and 21-28% for margin ratios across the full
    6,700-filer universe. Every saturated name becomes indistinguishable from every other, which
    destroys precisely the ranking the score exists to express.

    Setting the fence at the 99th percentile of |z| caps saturation at about 2% of any universe
    while keeping the map affine, so the ordering is untouched. Bounded below at 3 so a tight
    cross-section is not stretched, and above at 12 so one absurd outlier cannot flatten everyone.
    """
    finite = z.replace([np.inf, -np.inf], np.nan).dropna().abs()
    if finite.empty:
        return _Z_CLIP
    return float(np.clip(np.nanpercentile(finite, 99), 3.0, 12.0))


def _biweight_scale(values: pd.Series, centre: float, c: float = 9.0) -> float:
    """Tukey biweight scale — a robust spread estimator that survives a zero MAD.

    Falling back to a plain standard deviation when the MAD is zero reverts to the
    outlier-sensitive estimator ``robust_z`` exists to avoid, and a zero MAD means more than half
    the universe shares one value, which is exactly when outliers dominate a standard deviation.
    """
    clean = values.dropna()
    if len(clean) < 2:
        return 0.0
    deviations = clean - centre
    mad = float(deviations.abs().median())
    denominator = c * mad if mad > 0 else float(deviations.abs().mean())
    if denominator <= 0:
        return 0.0
    u = deviations / denominator
    mask = u.abs() < 1.0
    if mask.sum() < 2:
        return float(clean.std(ddof=1))
    numerator = ((deviations[mask] ** 2) * (1 - u[mask] ** 2) ** 4).sum()
    weight = ((1 - u[mask] ** 2) * (1 - 5 * u[mask] ** 2)).sum()
    if weight <= 0:
        return float(clean.std(ddof=1))
    return float(np.sqrt(len(clean) * numerator) / abs(weight))


def apply_direction(scores: pd.Series, direction: int) -> pd.Series:
    """Flip scores for lower-is-better metrics. Applied exactly once, by the caller of a normalizer."""
    if direction < 0:
        return 100.0 - scores
    return scores


@NORMALIZERS("zscore")
def zscore(values: pd.Series, **_: object) -> pd.Series:
    """Standard z-score. Fast, but one outlier consumes the standard deviation."""
    clean = values.dropna()
    if len(clean) < 2:
        return _neutral(values)
    sigma = float(clean.std(ddof=1))
    if sigma == 0.0 or not np.isfinite(sigma):
        return _neutral(values)
    return _to_score((values - float(clean.mean())) / sigma)


@NORMALIZERS("robust_z")
def robust_z(values: pd.Series, **_: object) -> pd.Series:
    """Median/MAD z-score — the sane default for financial ratios.

    Financial cross-sections are heavy-tailed and asymmetric: a handful of names with near-zero
    earnings produce P/E values in the thousands. The median and MAD are unmoved by them where the
    mean and standard deviation are not.
    """
    clean = values.dropna()
    if len(clean) < 2:
        return _neutral(values)
    median = float(clean.median())
    mad = float((clean - median).abs().median()) * _MAD_CONSISTENCY
    if mad == 0.0 or not np.isfinite(mad):
        # A zero MAD means at least half the universe shares one value. Falling back to a plain
        # z-score here would revert to the outlier-sensitive estimator this function exists to
        # avoid, in the very situation where outliers dominate it.
        scale = _biweight_scale(clean, median)
        if scale <= 0 or not np.isfinite(scale):
            # Biweight can also collapse — [1, 1, 1, 1, 1000] leaves no spread among the inliers.
            # A plain z-score is a poor estimator here but it still separates the outlier, and
            # returning a flat neutral would discard a real difference entirely.
            return zscore(values)
        mad = scale
    z = (values - median) / mad
    return _to_score(z, clip=_adaptive_clip(z))


@NORMALIZERS("winsorized_z")
def winsorized_z(
    values: pd.Series, *, lower: float = 0.01, upper: float = 0.99, k: float = 3.0, **_: object
) -> pd.Series:
    """Clamp the tails, then z-score.

    At ``n >= 100`` the clamp is the classic 1st/99th-quantile winsorisation. Below that — every
    peer group, since sector groups run 8-30 names and the letter floor is 15 — those quantiles
    interpolate to within a whisker of the observed min and max, so the clamp was a no-op exactly
    where one outlier does the most damage to the subsequent z-score. Small cross-sections clamp
    at ``median +/- k * MAD`` (sigma-consistent MAD) instead, which actually bites. A zero MAD
    keeps the quantile clamp: collapsing onto the median would erase a real difference that the
    quantile fence at least preserves.
    """
    clean = values.dropna()
    if len(clean) < 2:
        return _neutral(values)
    lo, hi = float(clean.quantile(lower)), float(clean.quantile(upper))
    if len(clean) < _WINSOR_SMALL_N:
        median = float(clean.median())
        mad = float((clean - median).abs().median()) * _MAD_CONSISTENCY
        if mad > 0.0 and np.isfinite(mad):
            lo, hi = median - k * mad, median + k * mad
    return zscore(values.clip(lo, hi))


@NORMALIZERS("percentile")
def percentile_rank(values: pd.Series, **_: object) -> pd.Series:
    """Percentile rank in 0..100. Fully distribution-free; discards magnitude information.

    Ties share the average rank, so a universe where 80% of names report zero R&D does not
    arbitrarily order them.
    """
    clean = values.dropna()
    if clean.empty:
        return pd.Series(np.nan, index=values.index, dtype="float64")
    if len(clean) == 1:
        return _neutral(values)
    # Hazen plotting position (rank - 0.5)/n — the same convention scoring.py
    # uses for grading percentiles. rank(pct=True) is r/n, which pins the top
    # observation to exactly 100 and skews the whole scale asymmetric; one
    # plotting position everywhere or ties break differently across layers.
    ranked = (values.rank(method="average") - 0.5) / float(len(clean)) * 100.0
    return ranked.astype("float64")


@NORMALIZERS("gaussian_rank")
def gaussian_rank(values: pd.Series, **_: object) -> pd.Series:
    """Van der Waerden transform: rank -> inverse normal CDF -> score.

    Combines the robustness of ranking with a bell-shaped output, so downstream averaging behaves
    and extreme ranks are separated more than middling ones.
    """
    clean = values.dropna()
    n = len(clean)
    if n < 2:
        return _neutral(values)
    ranks = values.rank(method="average")
    uniform = (ranks - 0.5) / n  # Blom-style continuity correction keeps the ends finite
    return _to_score(pd.Series(stats.norm.ppf(uniform.clip(1e-6, 1 - 1e-6)), index=values.index))


@NORMALIZERS("minmax")
def minmax(values: pd.Series, **_: object) -> pd.Series:
    """Linear rescale to 0..100. Maximally sensitive to outliers; included for completeness."""
    clean = values.dropna()
    if clean.empty:
        return pd.Series(np.nan, index=values.index, dtype="float64")
    low, high = float(clean.min()), float(clean.max())
    if high == low:
        return _neutral(values)
    return ((values - low) / (high - low) * 100.0).astype("float64")


@NORMALIZERS("sigmoid_z")
def sigmoid_z(values: pd.Series, *, steepness: float = 1.0, **_: object) -> pd.Series:
    """Logistic squash of a robust z-score. Saturates smoothly instead of clipping."""
    clean = values.dropna()
    if len(clean) < 2:
        return _neutral(values)
    median = float(clean.median())
    mad = float((clean - median).abs().median()) * _MAD_CONSISTENCY
    if mad == 0.0 or not np.isfinite(mad):
        return _neutral(values)
    z = (values - median) / mad
    return (100.0 / (1.0 + np.exp(-steepness * z))).astype("float64")


@NORMALIZERS("quantile_bucket")
def quantile_bucket(values: pd.Series, *, buckets: int = 10, **_: object) -> pd.Series:
    """Decile (or n-tile) scores. Coarse by design — robust to small data errors."""
    clean = values.dropna()
    if len(clean) < buckets:
        return percentile_rank(values)
    try:
        labels = pd.qcut(values, buckets, labels=False, duplicates="drop")
    except ValueError:
        return percentile_rank(values)
    max_label = float(np.nanmax(labels.to_numpy(dtype="float64")))
    if max_label <= 0:
        return _neutral(values)
    return labels.astype("float64") / max_label * 100.0


@NORMALIZERS("piecewise")
def piecewise(
    values: pd.Series, *, anchors: list[tuple[float, float]] | None = None, **_: object
) -> pd.Series:
    """Map raw values through calibrated ``(raw, score)`` anchor points by linear interpolation.

    The only normalizer that works with a universe of one, and the only one that is *absolute*:
    a 60% gross margin scores well whether or not the peer group is worse. Anchors come from the
    metric's own calibration in config, e.g. for return on equity::

        [(-0.20, 0), (0.0, 20), (0.10, 50), (0.20, 75), (0.40, 95), (0.80, 100)]

    Values outside the anchor range clamp to the end scores rather than extrapolating.
    """
    if not anchors:
        return _neutral(values)
    points = sorted(anchors, key=lambda p: p[0])
    xs = np.array([p[0] for p in points], dtype="float64")
    ys = np.array([p[1] for p in points], dtype="float64")
    raw = values.to_numpy(dtype="float64")
    out = np.interp(raw, xs, ys, left=ys[0], right=ys[-1])
    out = np.where(np.isnan(raw), np.nan, out)
    return pd.Series(out, index=values.index, dtype="float64")


@NORMALIZERS("double_sigmoid")
def double_sigmoid(
    values: pd.Series,
    *,
    ideal_band: tuple[float, float] = (0.0, 1.0),
    width: float | None = None,
    **_: object,
) -> pd.Series:
    """Score a **non-monotonic** metric where both extremes are bad.

    Inside ``ideal_band`` the score is 100, falling off smoothly on either side. This is the correct
    treatment for metrics with an optimum rather than a direction: a dividend payout ratio of 0%
    means no cash is returned, 95% means the dividend is unsustainable, and 40% is healthy. A
    monotonic normalizer would rank the 95% payer top of the dividend pillar.
    """
    low, high = float(ideal_band[0]), float(ideal_band[1])
    if high < low:
        low, high = high, low
    span = high - low
    scale = width if width is not None else (span if span > 0 else 1.0)
    if scale <= 0:
        scale = 1.0
    raw = values.to_numpy(dtype="float64")
    below = 1.0 / (1.0 + np.exp(-4.0 * (raw - low) / scale))
    above = 1.0 / (1.0 + np.exp(4.0 * (raw - high) / scale))
    out = 100.0 * np.minimum(below, above) * 2.0
    out = np.clip(out, 0.0, 100.0)
    out = np.where(np.isnan(raw), np.nan, out)
    return pd.Series(out, index=values.index, dtype="float64")


def sector_neutral(
    values: pd.Series,
    sectors: pd.Series,
    *,
    inner: str = "robust_z",
    min_group: int = 5,
    **kwargs: object,
) -> pd.Series:
    """Score within sector, shrinking small groups toward the global distribution.

    Comparing a utility's 4% net margin against a software company's 30% measures the industry, not
    the company. But a sector containing three names gives a meaningless within-group distribution,
    so groups are shrunk toward the global scoring with a James-Stein-flavoured weight
    ``n / (n + min_group)``: a 50-name sector is scored almost entirely within itself, a 2-name
    "sector" almost entirely globally.
    """
    inner_fn = NORMALIZERS.get(inner)
    global_scores = inner_fn(values, **kwargs)
    out = global_scores.copy()
    for sector, index in values.groupby(sectors).groups.items():
        subset = values.loc[index]
        if subset.dropna().empty:
            continue
        n = int(subset.notna().sum())
        local = inner_fn(subset, **kwargs)
        weight = n / (n + min_group)
        out.loc[index] = weight * local + (1.0 - weight) * global_scores.loc[index]
    return out


def normalize_series(
    values: pd.Series,
    *,
    method: str = "robust_z",
    direction: int = 1,
    sectors: pd.Series | None = None,
    ideal_band: tuple[float, float] | None = None,
    anchors: list[tuple[float, float]] | None = None,
    **kwargs: object,
) -> pd.Series:
    """Normalize one metric's cross-section into 0..100 scores, direction applied.

    Falls back to the absolute ``piecewise`` mapping when the universe is too small for a
    cross-sectional statistic to mean anything, rather than handing every name a flat 50.
    """
    if direction == 0:
        band = ideal_band or (0.0, 1.0)
        return double_sigmoid(values, ideal_band=band, **kwargs)

    usable = int(values.notna().sum())
    if usable < 2 and anchors:
        scores = piecewise(values, anchors=anchors)
    elif sectors is not None and method != "piecewise":
        scores = sector_neutral(values, sectors, inner=method, **kwargs)
    else:
        fn = NORMALIZERS.get(method)
        scores = (
            fn(values, anchors=anchors, **kwargs) if method == "piecewise" else fn(values, **kwargs)
        )

    return apply_direction(scores, direction)
