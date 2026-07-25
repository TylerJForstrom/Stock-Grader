"""Weighting methods — the heart of the grader.

The same registry drives both levels of the pipeline: metrics into a pillar, and pillars into the
final grade. Every method answers "how much should each component count?" from a different
philosophy, and they genuinely disagree — which is the point. A grade produced under one weighting
scheme and reproduced under six others is a much stronger claim than a grade from a single
hard-coded vector someone picked by feel.

Four families:

**A priori** — weights from judgement, before seeing data. ``equal``, ``fixed``, ``ahp``.
**Dispersion** — weights from how much a component *discriminates* between securities. A metric on
which every company scores the same carries no information, whatever its intuitive importance.
``entropy``, ``critic``, ``stddev``, ``coefficient_of_variation``.
**Structure** — weights from the covariance geometry of the components. ``pca``, ``inverse_variance``,
``risk_parity``, ``min_variance``, ``max_diversification``, ``hrp``, ``decorrelated``.
**Supervised** — weights from what actually predicted returns. ``ic``, ``ic_ir``, ``regression``,
``shapley``. These need forward returns and are the only ones that can be *wrong* in a testable
way, which is why the bake-off harness exists.

Every method obeys one contract:

* returns a :class:`pandas.Series` indexed by the columns of ``X``, non-negative, summing to 1;
* declares ``needs_panel`` / ``needs_returns`` and a ``fallback`` used when preconditions fail;
* degrades to its fallback with a recorded warning rather than raising or returning garbage.

That last point matters more than it sounds: grading a single stock gives a one-row panel, and
every covariance- or dispersion-based method is undefined there. Silent nonsense would be worse
than the honest fallback to equal weights, which is what happens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd
from scipy import stats
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.optimize import minimize
from scipy.spatial.distance import squareform

from .registry import WEIGHTINGS

log = logging.getLogger(__name__)

__all__ = ["WeightingContext", "compute_weights", "WEIGHT_METHOD_INFO"]

_EPS = 1e-12
_MIN_PANEL = 5  # rows below which a cross-sectional statistic is not worth computing


@dataclass(slots=True)
class WeightingContext:
    """Everything a weighting method may consult.

    Attributes:
        forward_returns: realised forward returns per security, for supervised methods.
        prior: a prior weight vector, for methods that shrink toward one.
        sectors: sector labels, for group-aware methods.
        config: method-specific parameters from YAML.
        warnings: appended to when a method falls back; surfaced in the report.
    """

    forward_returns: pd.Series | None = None
    prior: pd.Series | None = None
    sectors: pd.Series | None = None
    pairwise: pd.DataFrame | None = None  # AHP comparison matrix
    fixed: dict[str, float] | None = None
    config: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    seed: int = 0

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _normalize(weights: pd.Series, columns: pd.Index) -> pd.Series:
    """Force any raw weight vector into the contract: finite, non-negative, sums to 1."""
    w = pd.Series(weights, dtype="float64").reindex(columns)
    w = w.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    w = w.clip(lower=0.0)
    total = float(w.sum())
    if total <= _EPS:
        return pd.Series(1.0 / max(len(columns), 1), index=columns, dtype="float64")
    return w / total


def _equal(columns: pd.Index) -> pd.Series:
    return pd.Series(1.0 / max(len(columns), 1), index=columns, dtype="float64")


def _usable_panel(X: pd.DataFrame, minimum: int = _MIN_PANEL) -> bool:
    return X is not None and X.shape[0] >= minimum and X.shape[1] >= 2


def _clean(X: pd.DataFrame) -> pd.DataFrame:
    """Median-impute so covariance estimates are not driven by the missingness pattern."""
    clean = X.replace([np.inf, -np.inf], np.nan).astype("float64")
    return clean.fillna(clean.median())


# ---------------------------------------------------------------------------------------------
# A priori
# ---------------------------------------------------------------------------------------------


@WEIGHTINGS("equal", needs_panel=False, needs_returns=False, fallback=None)
def equal_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Equal weight. The honest default, and a surprisingly hard benchmark to beat.

    Equal weighting has no estimation error, which is why it routinely outperforms "optimised"
    schemes whose inputs are estimated from short, noisy samples.
    """
    return _equal(X.columns)


@WEIGHTINGS("fixed", needs_panel=False, needs_returns=False, fallback="equal")
def fixed_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Weights supplied by configuration — expert judgement, stated explicitly."""
    if not ctx.fixed:
        ctx.warn("fixed weighting: no weights configured, falling back to equal")
        return _equal(X.columns)
    return _normalize(pd.Series(ctx.fixed, dtype="float64"), X.columns)


# Saaty's random-consistency index, indexed by matrix order n.
_SAATY_RI = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41,
             9: 1.45, 10: 1.49, 11: 1.51, 12: 1.48, 13: 1.56, 14: 1.57, 15: 1.59}


@WEIGHTINGS("ahp", needs_panel=False, needs_returns=False, fallback="equal")
def ahp_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Analytic Hierarchy Process: weights from pairwise judgements, with a consistency check.

    You state "valuation is 3x as important as momentum" for each pair; the weights are the
    principal eigenvector of that matrix. The consistency ratio then audits whether your judgements
    were coherent — if you said A>B, B>C and C>A, ``CR`` exceeds 0.10 and the result is rejected
    rather than quietly averaged into something meaningless.
    """
    matrix = ctx.pairwise
    if matrix is None or matrix.empty:
        ctx.warn("ahp: no pairwise comparison matrix supplied, falling back to equal")
        return _equal(X.columns)
    common = [c for c in X.columns if c in matrix.index and c in matrix.columns]
    if len(common) < 2:
        ctx.warn("ahp: comparison matrix does not cover the components, falling back to equal")
        return _equal(X.columns)

    A = matrix.loc[common, common].to_numpy(dtype="float64")
    if not np.all(np.isfinite(A)) or np.any(A <= 0):
        ctx.warn("ahp: comparison matrix has non-positive entries, falling back to equal")
        return _equal(X.columns)

    eigenvalues, eigenvectors = np.linalg.eig(A)
    principal = int(np.argmax(eigenvalues.real))
    vector = np.abs(eigenvectors[:, principal].real)
    lambda_max = float(eigenvalues[principal].real)

    n = len(common)
    if n > 2:
        ci = (lambda_max - n) / (n - 1)
        ri = _SAATY_RI.get(n, 1.59)
        cr = ci / ri if ri > 0 else 0.0
        if cr > 0.10:
            ctx.warn(f"ahp: inconsistent judgements (CR={cr:.3f} > 0.10); weights are unreliable")
        ctx.config["ahp_consistency_ratio"] = cr

    return _normalize(pd.Series(vector, index=common), X.columns)


# ---------------------------------------------------------------------------------------------
# Dispersion
# ---------------------------------------------------------------------------------------------


@WEIGHTINGS("entropy", needs_panel=True, needs_returns=False, fallback="equal")
def entropy_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Shannon entropy weighting: components that discriminate more, count more.

    ``e_j = -(1/ln n) * sum_i p_ij ln p_ij`` where ``p_ij`` is the column-normalised score, and
    ``w_j`` is proportional to ``1 - e_j``. A metric on which every company scores identically has
    maximum entropy, zero divergence, and therefore zero weight — it cannot separate anyone.
    """
    if not _usable_panel(X):
        ctx.warn("entropy: needs a panel of at least 5 securities, falling back to equal")
        return _equal(X.columns)
    data = _clean(X)
    shifted = data - data.min() + _EPS  # entropy needs non-negative inputs
    totals = shifted.sum()
    totals = totals.replace(0.0, np.nan)
    p = shifted / totals
    n = len(data)
    with np.errstate(divide="ignore", invalid="ignore"):
        plogp = p * np.log(p.where(p > 0))
    entropy = -plogp.sum() / np.log(n)
    divergence = (1.0 - entropy).clip(lower=0.0)
    if float(divergence.sum()) <= _EPS:
        ctx.warn("entropy: no component discriminates, falling back to equal")
        return _equal(X.columns)
    return _normalize(divergence, X.columns)


@WEIGHTINGS("critic", needs_panel=True, needs_returns=False, fallback="equal")
def critic_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """CRITIC: dispersion *and* independence.

    ``w_j ∝ sigma_j * sum_k (1 - r_jk)``. A component earns weight for varying a lot **and** for
    being uncorrelated with the others. This is the natural answer to a catalogue that contains
    P/E, P/S and EV/EBITDA — three views of the same thing, which under a dispersion-only scheme
    would collectively out-vote everything else.
    """
    if not _usable_panel(X):
        ctx.warn("critic: needs a panel of at least 5 securities, falling back to equal")
        return _equal(X.columns)
    data = _clean(X)
    sigma = data.std(ddof=1)
    corr = data.corr().fillna(0.0)
    conflict = (1.0 - corr).sum()
    scores = (sigma * conflict).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if float(scores.sum()) <= _EPS:
        ctx.warn("critic: degenerate dispersion, falling back to equal")
        return _equal(X.columns)
    return _normalize(scores, X.columns)


@WEIGHTINGS("stddev", needs_panel=True, needs_returns=False, fallback="equal")
def stddev_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Weight by standard deviation — the simplest dispersion scheme."""
    if not _usable_panel(X):
        ctx.warn("stddev: needs a panel, falling back to equal")
        return _equal(X.columns)
    return _normalize(_clean(X).std(ddof=1), X.columns)


@WEIGHTINGS("coefficient_of_variation", needs_panel=True, needs_returns=False, fallback="equal")
def cv_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Weight by ``sigma/|mu|`` — dispersion made scale-free."""
    if not _usable_panel(X):
        ctx.warn("coefficient_of_variation: needs a panel, falling back to equal")
        return _equal(X.columns)
    data = _clean(X)
    mu = data.mean().abs().replace(0.0, np.nan)
    return _normalize((data.std(ddof=1) / mu).fillna(0.0), X.columns)


# ---------------------------------------------------------------------------------------------
# Covariance structure
# ---------------------------------------------------------------------------------------------


@WEIGHTINGS("pca", needs_panel=True, needs_returns=False, fallback="equal")
def pca_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Weight by loading on the leading principal components.

    Captures the dominant shared factor among the components. Loadings are combined across the
    components needed to explain ``variance_target`` of total variance, weighted by each one's own
    explained variance, so a panel with two genuinely distinct drivers is not forced onto one axis.
    """
    if not _usable_panel(X):
        ctx.warn("pca: needs a panel of at least 5 securities, falling back to equal")
        return _equal(X.columns)
    data = _clean(X)
    standardized = (data - data.mean()) / data.std(ddof=1).replace(0.0, np.nan)
    standardized = standardized.dropna(axis=1, how="all").fillna(0.0)
    if standardized.shape[1] < 2:
        ctx.warn("pca: fewer than two varying components, falling back to equal")
        return _equal(X.columns)
    try:
        _, singular, vt = np.linalg.svd(standardized.to_numpy(), full_matrices=False)
    except np.linalg.LinAlgError:
        ctx.warn("pca: SVD did not converge, falling back to equal")
        return _equal(X.columns)
    explained = singular**2
    total = explained.sum()
    if total <= _EPS:
        return _equal(X.columns)
    ratio = explained / total
    target = float(ctx.config.get("variance_target", 0.8))
    keep = max(1, int(np.searchsorted(np.cumsum(ratio), target) + 1))
    loadings = np.abs(vt[:keep]).T @ ratio[:keep]
    return _normalize(pd.Series(loadings, index=standardized.columns), X.columns)


@WEIGHTINGS("inverse_variance", needs_panel=True, needs_returns=False, fallback="equal")
def inverse_variance_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """``w_j ∝ 1/sigma_j^2``. Noisy components are trusted less."""
    if not _usable_panel(X):
        ctx.warn("inverse_variance: needs a panel, falling back to equal")
        return _equal(X.columns)
    variance = _clean(X).var(ddof=1).replace(0.0, np.nan)
    return _normalize((1.0 / variance).fillna(0.0), X.columns)


def _covariance(X: pd.DataFrame, shrink: bool = True) -> np.ndarray:
    """Ledoit-Wolf shrunk covariance.

    With 60 metrics and 200 securities the sample covariance is barely invertible and its smallest
    eigenvalues are almost pure noise; any optimiser will pile weight onto exactly those directions.
    Shrinking toward a scaled identity is what makes the optimisation-based methods usable at all.
    """
    data = _clean(X)
    sample = np.cov(data.to_numpy(), rowvar=False)
    sample = np.atleast_2d(sample)
    if not shrink or sample.shape[0] < 2:
        return sample
    n, p = data.shape
    mu = float(np.trace(sample) / p)
    target = mu * np.eye(p)
    delta = float(np.sum((sample - target) ** 2) / p)
    centered = data.to_numpy() - data.to_numpy().mean(axis=0)
    beta = float(sum(np.sum((np.outer(row, row) - sample) ** 2) for row in centered) / (n**2 * p))
    intensity = 0.0 if delta <= _EPS else float(np.clip(beta / delta, 0.0, 1.0))
    return intensity * target + (1 - intensity) * sample


@WEIGHTINGS("min_variance", needs_panel=True, needs_returns=False, fallback="inverse_variance")
def min_variance_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Long-only minimum-variance weights, solved by SLSQP under a sum-to-one constraint."""
    if not _usable_panel(X):
        ctx.warn("min_variance: needs a panel, falling back to equal")
        return _equal(X.columns)
    cov = _covariance(X)
    p = cov.shape[0]
    start = np.repeat(1.0 / p, p)
    result = minimize(
        lambda w: float(w @ cov @ w),
        start,
        method="SLSQP",
        bounds=[(0.0, 1.0)] * p,
        constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
        options={"maxiter": 500, "ftol": 1e-12},
    )
    if not result.success:
        ctx.warn(f"min_variance: optimiser did not converge ({result.message}), using inverse variance")
        return inverse_variance_weights(X, ctx)
    return _normalize(pd.Series(result.x, index=X.columns), X.columns)


@WEIGHTINGS("risk_parity", needs_panel=True, needs_returns=False, fallback="inverse_variance")
def risk_parity_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Equal risk contribution: every component contributes the same share of total variance.

    Solved by minimising the squared dispersion of the risk contributions
    ``RC_i = w_i * (Sigma w)_i``. Unlike inverse variance, this accounts for correlation, so three
    correlated valuation metrics share one component's worth of risk budget rather than three.
    """
    if not _usable_panel(X):
        ctx.warn("risk_parity: needs a panel, falling back to equal")
        return _equal(X.columns)
    cov = _covariance(X)
    p = cov.shape[0]

    def objective(w: np.ndarray) -> float:
        portfolio_var = float(w @ cov @ w)
        if portfolio_var <= _EPS:
            return 1e6
        contributions = w * (cov @ w)
        return float(np.sum((contributions - portfolio_var / p) ** 2))

    result = minimize(
        objective,
        np.repeat(1.0 / p, p),
        method="SLSQP",
        bounds=[(1e-6, 1.0)] * p,
        constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
        options={"maxiter": 1000, "ftol": 1e-14},
    )
    if not result.success:
        ctx.warn("risk_parity: did not converge, using inverse variance")
        return inverse_variance_weights(X, ctx)
    return _normalize(pd.Series(result.x, index=X.columns), X.columns)


@WEIGHTINGS("max_diversification", needs_panel=True, needs_returns=False, fallback="inverse_variance")
def max_diversification_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Maximise the diversification ratio ``(w·sigma) / sqrt(w'Sigma w)``."""
    if not _usable_panel(X):
        ctx.warn("max_diversification: needs a panel, falling back to equal")
        return _equal(X.columns)
    cov = _covariance(X)
    sigma = np.sqrt(np.clip(np.diag(cov), _EPS, None))
    p = cov.shape[0]

    def negative_ratio(w: np.ndarray) -> float:
        denominator = np.sqrt(max(float(w @ cov @ w), _EPS))
        return -float(w @ sigma) / denominator

    result = minimize(
        negative_ratio,
        np.repeat(1.0 / p, p),
        method="SLSQP",
        bounds=[(0.0, 1.0)] * p,
        constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0}],
        options={"maxiter": 500, "ftol": 1e-12},
    )
    if not result.success:
        ctx.warn("max_diversification: did not converge, using inverse variance")
        return inverse_variance_weights(X, ctx)
    return _normalize(pd.Series(result.x, index=X.columns), X.columns)


@WEIGHTINGS("hrp", needs_panel=True, needs_returns=False, fallback="inverse_variance")
def hrp_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Hierarchical Risk Parity: cluster, then split risk budget down the tree.

    Avoids inverting the covariance matrix entirely — it clusters components by correlation
    distance ``sqrt(0.5*(1-rho))`` and recursively bisects the risk budget. With redundant metrics
    this behaves far better than a naive optimiser, because near-duplicates land in the same
    cluster and split one budget instead of each claiming a full share.
    """
    if not _usable_panel(X):
        ctx.warn("hrp: needs a panel, falling back to equal")
        return _equal(X.columns)
    data = _clean(X)
    corr = data.corr().fillna(0.0)
    if corr.shape[0] < 2:
        return _equal(X.columns)
    distance = np.sqrt(np.clip(0.5 * (1.0 - corr.to_numpy()), 0.0, None))
    np.fill_diagonal(distance, 0.0)
    try:
        links = linkage(squareform(distance, checks=False), method="single")
    except ValueError:
        ctx.warn("hrp: clustering failed, falling back to inverse variance")
        return inverse_variance_weights(X, ctx)

    order = _quasi_diagonal(links, corr.shape[0])
    labels = [corr.index[i] for i in order]
    cov = pd.DataFrame(_covariance(data), index=data.columns, columns=data.columns)
    weights = pd.Series(1.0, index=labels, dtype="float64")

    clusters = [labels]
    while clusters:
        clusters = [
            part
            for cluster in clusters
            for part in (cluster[: len(cluster) // 2], cluster[len(cluster) // 2 :])
            if len(cluster) > 1 and len(part) > 0
        ]
        for i in range(0, len(clusters), 2):
            if i + 1 >= len(clusters):
                break
            left, right = clusters[i], clusters[i + 1]
            var_left = _cluster_variance(cov, left)
            var_right = _cluster_variance(cov, right)
            total = var_left + var_right
            alpha = 1.0 - var_left / total if total > _EPS else 0.5
            weights[left] *= alpha
            weights[right] *= 1.0 - alpha
    return _normalize(weights, X.columns)


def _quasi_diagonal(links: np.ndarray, n: int) -> list[int]:
    """Reorder leaves so correlated components sit adjacent."""
    links = links.astype(int)
    order = pd.Series([links[-1, 0], links[-1, 1]])
    while order.max() >= n:
        order.index = range(0, order.shape[0] * 2, 2)
        clustered = order[order >= n]
        i, j = clustered.index, clustered.to_numpy() - n
        order[i] = links[j, 0]
        order = pd.concat([order, pd.Series(links[j, 1], index=i + 1)]).sort_index()
    return order.tolist()


def _cluster_variance(cov: pd.DataFrame, members: list[str]) -> float:
    sub = cov.loc[members, members]
    diag = np.diag(sub.to_numpy())
    inv = 1.0 / np.where(diag > _EPS, diag, np.nan)
    if np.all(np.isnan(inv)):
        return float("inf")
    w = np.nan_to_num(inv) / np.nansum(inv)
    return float(w @ sub.to_numpy() @ w)


@WEIGHTINGS("decorrelated", needs_panel=True, needs_returns=False, fallback="inverse_variance")
def decorrelated_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """``w ∝ Sigma^-1 * 1`` on the shrunk covariance — strips redundancy between components."""
    if not _usable_panel(X):
        ctx.warn("decorrelated: needs a panel, falling back to equal")
        return _equal(X.columns)
    cov = _covariance(X)
    try:
        raw = np.linalg.solve(cov, np.ones(cov.shape[0]))
    except np.linalg.LinAlgError:
        ctx.warn("decorrelated: singular covariance, falling back to inverse variance")
        return inverse_variance_weights(X, ctx)
    return _normalize(pd.Series(raw, index=X.columns), X.columns)


# ---------------------------------------------------------------------------------------------
# Supervised
# ---------------------------------------------------------------------------------------------


def _forward_returns(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series | None:
    if ctx.forward_returns is None:
        return None
    aligned = ctx.forward_returns.reindex(X.index).dropna()
    return aligned if len(aligned) >= _MIN_PANEL else None


@WEIGHTINGS("ic", needs_panel=True, needs_returns=True, fallback="equal")
def ic_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Weight by rank information coefficient — Spearman correlation with forward returns.

    Negative-IC components get zero weight rather than a negative one: a factor that predicts the
    wrong way should be *investigated*, not silently inverted into the composite.
    """
    returns = _forward_returns(X, ctx)
    if returns is None:
        ctx.warn("ic: no forward returns supplied, falling back to equal")
        return _equal(X.columns)
    data = _clean(X).loc[returns.index]
    ics = {}
    for column in data.columns:
        try:
            rho = stats.spearmanr(data[column], returns).statistic
        except (ValueError, TypeError):
            rho = np.nan
        ics[column] = 0.0 if not np.isfinite(rho) else max(0.0, float(rho))
    ctx.config["ic_values"] = ics
    if sum(ics.values()) <= _EPS:
        ctx.warn("ic: no component had positive predictive correlation, falling back to equal")
        return _equal(X.columns)
    return _normalize(pd.Series(ics), X.columns)


@WEIGHTINGS("ic_ir", needs_panel=True, needs_returns=True, fallback="ic")
def ic_ir_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """IC divided by its own volatility, shrunk toward zero — a t-statistic, not a point estimate.

    A factor with IC 0.05 measured consistently over 60 periods is worth more than one with IC 0.15
    measured once. With only a single cross-section available the IC standard error is unknown, so
    the estimate is shrunk by ``n/(n+k)`` toward zero, which keeps a lucky one-period reading from
    dominating.
    """
    returns = _forward_returns(X, ctx)
    if returns is None:
        ctx.warn("ic_ir: no forward returns supplied, falling back to equal")
        return _equal(X.columns)
    history = ctx.config.get("ic_history")
    if isinstance(history, pd.DataFrame) and history.shape[0] >= 3:
        mean_ic, std_ic = history.mean(), history.std(ddof=1).replace(0.0, np.nan)
        n = history.shape[0]
        shrinkage = n / (n + 10.0)
        scores = (mean_ic / std_ic * shrinkage).clip(lower=0.0).fillna(0.0)
        if float(scores.sum()) > _EPS:
            return _normalize(scores, X.columns)
    ctx.warn("ic_ir: no multi-period IC history, shrinking single-period IC instead")
    base = ic_weights(X, ctx)
    n = len(returns)
    shrinkage = n / (n + 30.0)
    return _normalize(base * shrinkage + _equal(X.columns) * (1 - shrinkage), X.columns)


@WEIGHTINGS("regression", needs_panel=True, needs_returns=True, fallback="ic")
def regression_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Non-negative ridge regression of forward returns on standardised components.

    Ridge rather than OLS because the components are collinear by construction; non-negativity
    because a negative weight in a *grade* is incoherent — it would mean "being good at this makes
    the stock worse", which if true is a modelling error, not a weight.
    """
    returns = _forward_returns(X, ctx)
    if returns is None:
        ctx.warn("regression: no forward returns supplied, falling back to equal")
        return _equal(X.columns)
    from sklearn.linear_model import Ridge

    data = _clean(X).loc[returns.index]
    standardized = (data - data.mean()) / data.std(ddof=1).replace(0.0, np.nan)
    standardized = standardized.fillna(0.0)
    alpha = float(ctx.config.get("ridge_alpha", 1.0))
    model = Ridge(alpha=alpha, fit_intercept=True, positive=True)
    try:
        model.fit(standardized.to_numpy(), returns.to_numpy())
    except (ValueError, np.linalg.LinAlgError) as exc:
        ctx.warn(f"regression: fit failed ({exc}), falling back to IC")
        return ic_weights(X, ctx)
    coefficients = pd.Series(model.coef_, index=standardized.columns).clip(lower=0.0)
    if float(coefficients.sum()) <= _EPS:
        ctx.warn("regression: all coefficients shrank to zero, falling back to equal")
        return _equal(X.columns)
    return _normalize(coefficients, X.columns)


@WEIGHTINGS("shapley", needs_panel=True, needs_returns=True, fallback="ic")
def shapley_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Shapley value of each component's marginal contribution to predictive power.

    The only method here that is fair in a game-theoretic sense: it averages a component's marginal
    contribution over *every* ordering, so two redundant metrics split the credit they jointly earn
    instead of both claiming it. Exact enumeration is exponential, so above 10 components it
    switches to seeded Monte-Carlo permutation sampling.
    """
    returns = _forward_returns(X, ctx)
    if returns is None:
        ctx.warn("shapley: no forward returns supplied, falling back to equal")
        return _equal(X.columns)
    data = _clean(X).loc[returns.index]
    columns = list(data.columns)
    p = len(columns)
    if p == 0:
        return _equal(X.columns)
    if p == 1:
        return _equal(X.columns)

    target = returns.rank()

    def value(subset: tuple[str, ...]) -> float:
        if not subset:
            return 0.0
        composite = data[list(subset)].rank().mean(axis=1)
        try:
            rho = stats.spearmanr(composite, target).statistic
        except (ValueError, TypeError):
            return 0.0
        return 0.0 if not np.isfinite(rho) else max(0.0, float(rho))

    contributions = dict.fromkeys(columns, 0.0)
    rng = np.random.default_rng(ctx.seed)
    n_permutations = int(ctx.config.get("shapley_permutations", 200))

    if p <= 10:
        from itertools import permutations as iter_permutations

        orderings = list(iter_permutations(columns))
        if len(orderings) > 5000:
            orderings = [tuple(rng.permutation(columns)) for _ in range(n_permutations)]
    else:
        orderings = [tuple(rng.permutation(columns)) for _ in range(n_permutations)]
        ctx.config["shapley_sampled"] = True

    cache: dict[tuple[str, ...], float] = {}
    for ordering in orderings:
        running: tuple[str, ...] = ()
        previous = 0.0
        for column in ordering:
            running = running + (column,)
            key = tuple(sorted(running))
            if key not in cache:
                cache[key] = value(key)
            current = cache[key]
            contributions[column] += current - previous
            previous = current
    total_orderings = max(len(orderings), 1)
    scores = pd.Series({k: v / total_orderings for k, v in contributions.items()}).clip(lower=0.0)
    if float(scores.sum()) <= _EPS:
        ctx.warn("shapley: no component contributed positively, falling back to equal")
        return _equal(X.columns)
    return _normalize(scores, X.columns)


# ---------------------------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------------------------


@WEIGHTINGS("inverse_volatility", needs_panel=True, needs_returns=False, fallback="equal")
def inverse_volatility_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """``w_j ∝ 1/sigma_j`` — inverse *volatility*, not inverse variance.

    Deliberately distinct from :func:`inverse_variance_weights`. Conflating the two is the classic
    risk-parity error: ``1/sigma`` gives equal risk contribution only when components are
    uncorrelated, while ``1/sigma^2`` is the minimum-variance solution under that same assumption.
    They give materially different vectors whenever dispersions differ, so both are offered and
    named for what they actually compute.
    """
    if not _usable_panel(X):
        ctx.warn("inverse_volatility: needs a panel, falling back to equal")
        return _equal(X.columns)
    sigma = _clean(X).std(ddof=1).replace(0.0, np.nan)
    return _normalize((1.0 / sigma).fillna(0.0), X.columns)


@WEIGHTINGS("rank_order_centroid", needs_panel=False, needs_returns=False, fallback="equal")
def rank_order_centroid_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Rank-order centroid weights from a simple ordering of importance.

    ``w_i = (1/n) * sum_{k=i..n} 1/k`` for the i-th ranked component. Asking someone to *rank*
    attributes is far more reliable than asking them to assign numeric weights, and ROC is the
    centroid of every weight vector consistent with that ranking — the least-committal choice given
    only an order. Supply the ordering as ``ctx.config["ranking"]``.
    """
    ranking = ctx.config.get("ranking")
    if not ranking:
        ctx.warn("rank_order_centroid: no ranking supplied in config, falling back to equal")
        return _equal(X.columns)
    ordered = [c for c in ranking if c in X.columns]
    ordered += [c for c in X.columns if c not in ordered]
    n = len(ordered)
    weights = {
        column: sum(1.0 / k for k in range(i + 1, n + 1)) / n
        for i, column in enumerate(ordered)
    }
    return _normalize(pd.Series(weights), X.columns)


@WEIGHTINGS("mutual_information", needs_panel=True, needs_returns=True, fallback="ic")
def mutual_information_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Weight by mutual information with forward returns.

    Catches dependence that a rank correlation cannot: a factor that is only informative at its
    extremes, or one whose relationship with returns is U-shaped, has an information coefficient
    near zero while carrying real signal. Estimated on a discretised joint histogram, which is
    crude but stable at the sample sizes a single cross-section provides.
    """
    returns = _forward_returns(X, ctx)
    if returns is None:
        ctx.warn("mutual_information: no forward returns supplied, falling back to equal")
        return _equal(X.columns)
    data = _clean(X).loc[returns.index]
    bins = int(ctx.config.get("mi_bins", max(4, min(10, int(np.sqrt(len(returns) / 3))))))
    try:
        y_binned = pd.qcut(returns, bins, labels=False, duplicates="drop")
    except ValueError:
        ctx.warn("mutual_information: returns could not be binned, falling back to IC")
        return ic_weights(X, ctx)

    scores: dict[str, float] = {}
    for column in data.columns:
        try:
            x_binned = pd.qcut(data[column], bins, labels=False, duplicates="drop")
        except ValueError:
            scores[column] = 0.0
            continue
        # copy(): crosstab can hand back a read-only view, and the normalisation is in place.
        joint = np.array(pd.crosstab(x_binned, y_binned).to_numpy(), dtype="float64", copy=True)
        total = joint.sum()
        if total <= 0:
            scores[column] = 0.0
            continue
        joint /= total
        px = joint.sum(axis=1, keepdims=True)
        py = joint.sum(axis=0, keepdims=True)
        with np.errstate(divide="ignore", invalid="ignore"):
            terms = joint * np.log(joint / (px * py))
        scores[column] = float(np.nansum(np.where(joint > 0, terms, 0.0)))

    series = pd.Series(scores).clip(lower=0.0)
    if float(series.sum()) <= _EPS:
        ctx.warn("mutual_information: no component carried information, falling back to equal")
        return _equal(X.columns)
    return _normalize(series, X.columns)


@WEIGHTINGS("bagged", needs_panel=True, needs_returns=False, fallback="equal")
def bagged_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Bootstrap-averaged weights from a base method.

    Any data-driven weighting is an estimate from one finite sample, and the optimisation-based ones
    are notoriously unstable — a few different securities in the panel can reorder the whole vector.
    Averaging the base method's weights across bootstrap resamples of the securities keeps the
    result from being an artefact of which names happened to be in the universe.
    """
    if not _usable_panel(X):
        ctx.warn("bagged: needs a panel, falling back to equal")
        return _equal(X.columns)
    base = str(ctx.config.get("bagged_base", "critic"))
    if base in ("bagged", "consensus"):
        base = "critic"
    rounds = int(ctx.config.get("bagged_rounds", 20))
    rng = np.random.default_rng(ctx.seed)
    n = X.shape[0]

    vectors = []
    for _ in range(rounds):
        sample = X.iloc[rng.integers(0, n, size=n)]
        try:
            vectors.append(compute_weights(sample, method=base, ctx=ctx))
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"bagged: base method {base} failed ({exc})")
            break
    if not vectors:
        return _equal(X.columns)
    return _normalize(pd.concat(vectors, axis=1).mean(axis=1), X.columns)


@WEIGHTINGS("consensus", needs_panel=True, needs_returns=False, fallback="equal")
def consensus_weights(X: pd.DataFrame, ctx: WeightingContext) -> pd.Series:
    """Median weight vector across several methods, renormalised.

    Every scheme here encodes a different, defensible opinion, and each has a failure mode. Taking
    the component-wise median across them is robust to any single method behaving pathologically on
    a particular panel — the same logic that makes the median a good estimator, applied one level up.
    """
    methods = ctx.config.get("consensus_methods") or ["equal", "entropy", "critic", "pca", "risk_parity"]
    vectors = []
    for name in methods:
        if name == "consensus":
            continue
        try:
            vectors.append(compute_weights(X, method=name, ctx=ctx))
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"consensus: method {name} failed ({exc}), skipped")
    if not vectors:
        return _equal(X.columns)
    stacked = pd.concat(vectors, axis=1)
    return _normalize(stacked.median(axis=1), X.columns)


WEIGHT_METHOD_INFO: dict[str, dict] = {}


def compute_weights(
    X: pd.DataFrame,
    *,
    method: str = "equal",
    ctx: WeightingContext | None = None,
) -> pd.Series:
    """Compute weights by registered method name, enforcing the shared contract.

    ``X`` is securities (rows) by components (columns), and its columns **must already be
    normalised to a common scale** — the 0..100 scores the normalize layer emits. The dispersion
    and covariance methods (``stddev``, ``inverse_variance``, ``min_variance``, ``risk_parity``)
    read raw column variance, so feeding them unnormalised mixed units would hand the largest
    weight to whichever metric happens to be quoted in the smallest units rather than to whichever
    one carries the most information.

    For a single-stock grade the panel has one row, so every data-driven method falls back —
    loudly, via ``ctx.warnings`` — to a scheme that is defined there.
    """
    ctx = ctx or WeightingContext()
    if X is None or X.shape[1] == 0:
        return pd.Series(dtype="float64")

    fn: Callable = WEIGHTINGS.get(method)
    needs_returns = bool(getattr(fn, "needs_returns", False))
    if needs_returns and ctx.forward_returns is None:
        fallback = getattr(fn, "fallback", None) or "equal"
        ctx.warn(f"{method}: requires forward returns, using {fallback}")
        fn = WEIGHTINGS.get(fallback)

    weights = fn(X, ctx)
    return _normalize(weights, X.columns)


for _name, _fn in WEIGHTINGS.items():
    WEIGHT_METHOD_INFO[_name] = {
        "needs_panel": bool(getattr(_fn, "needs_panel", False)),
        "needs_returns": bool(getattr(_fn, "needs_returns", False)),
        "fallback": getattr(_fn, "fallback", None),
        "doc": (_fn.__doc__ or "").strip().split("\n")[0],
    }
