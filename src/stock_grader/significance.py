"""Statistical significance of a trading edge — pure Python, no SciPy.

Ported verbatim from the Stock Market Simulation project's battle-tested
harness (its positive/negative-control tests port alongside it); only the
metrics-helper import changed. See REVISED_PLAN §6.

A high backtest Sharpe is almost meaningless on its own: if you tried many
strategy configurations and kept the best, the winner's Sharpe is inflated by
*selection*, and the Sharpe statistic itself is distorted by skew and fat tails.
This module corrects for both, so "is there an edge?" gets an honest yes/no.

Key tools (Bailey & Lopez de Prado, 2012/2014):

- **Probabilistic Sharpe Ratio (PSR)**: P(true Sharpe > benchmark) given the
  observed Sharpe, sample length, skew and kurtosis.
- **Deflated Sharpe Ratio (DSR)**: PSR where the benchmark is the *expected
  maximum* Sharpe under the null of no skill across N trials. DSR >= 0.95 means
  the best-of-N strategy's Sharpe survives the multiple-testing correction.
- **Block-bootstrap CI** on the (annualized) Sharpe, preserving autocorrelation.

All Sharpe inputs to PSR/DSR are **per-period** (not annualized); the helpers
here keep that straight. Diagnostics, not optimisation targets.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from .validation_stats import excess_kurtosis, mean, sharpe_ratio, stdev

__all__ = [
    "EULER_MASCHERONI",
    "SignificanceReport",
    "assess_edge",
    "block_bootstrap_sharpe_ci",
    "deflated_sharpe_ratio",
    "expected_max_sharpe",
    "norm_cdf",
    "norm_ppf",
    "per_period_sharpe",
    "probabilistic_sharpe_ratio",
    "skewness",
]

EULER_MASCHERONI = 0.5772156649015329


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation).

    Accurate to ~1e-9 on (0, 1); good enough for significance thresholds.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def skewness(xs: Sequence[float]) -> float:
    """Population skewness (normal = 0)."""
    n = len(xs)
    if n < 3:
        return 0.0
    mu = mean(xs)
    sd = stdev(xs, sample=False)
    if sd == 0:
        return 0.0
    return (sum((x - mu) ** 3 for x in xs) / n) / sd**3


def per_period_sharpe(returns: Sequence[float]) -> float:
    """Non-annualized Sharpe (mean/std of returns). The PSR/DSR convention."""
    if len(returns) < 2:
        return 0.0
    sd = stdev(returns)
    return mean(returns) / sd if sd > 0 else 0.0


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark_sr: float = 0.0) -> float:
    """P(true per-period Sharpe > ``benchmark_sr``), skew/kurtosis-aware."""
    n = len(returns)
    if n < 2:
        return 0.0
    sr = per_period_sharpe(returns)
    g3 = skewness(returns)
    g4 = excess_kurtosis(returns) + 3.0  # non-excess kurtosis (normal = 3)
    denom_sq = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0.0:
        return 0.0
    z = (sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return norm_cdf(z)


def expected_max_sharpe(trial_sharpe_std: float, n_trials: int) -> float:
    """Expected maximum per-period Sharpe under the null of no skill (N trials).

    This is the benchmark a best-of-N Sharpe must clear to be credible.
    """
    if n_trials < 2 or trial_sharpe_std <= 0.0:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return trial_sharpe_std * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def _finite_trial_sharpes(trial_sharpes: Sequence[float | None]) -> list[float]:
    """Drop None/non-finite entries: one NaN makes stdev — and so every DSR — NaN."""
    return [
        float(sharpe)
        for sharpe in trial_sharpes
        if isinstance(sharpe, (int, float)) and math.isfinite(sharpe)
    ]


def deflated_sharpe_ratio(
    returns: Sequence[float], trial_sharpes: Sequence[float | None]
) -> tuple[float, float]:
    """Deflated Sharpe Ratio.

    Returns ``(dsr, deflated_benchmark_sr)``. ``trial_sharpes`` are the
    *per-period* Sharpes of every configuration searched (their dispersion sets
    the selection-inflation benchmark). None/non-finite entries — trials whose
    Sharpe could not be computed — are excluded from the dispersion estimate.
    """
    usable = _finite_trial_sharpes(trial_sharpes)
    n_trials = len(usable)
    benchmark = expected_max_sharpe(stdev(usable), n_trials)
    return probabilistic_sharpe_ratio(returns, benchmark_sr=benchmark), benchmark


def block_bootstrap_sharpe_ci(
    returns: Sequence[float],
    *,
    n_boot: int = 2000,
    block: int = 10,
    alpha: float = 0.05,
    seed: int = 0,
    periods_per_year: int = 252,
) -> tuple[float, float]:
    """Circular block-bootstrap CI on the ANNUALIZED Sharpe (preserves autocorr)."""
    n = len(returns)
    if n < block + 1:
        return (0.0, 0.0)
    rng = random.Random(seed)
    full_blocks = n // block
    remainder = n % block
    full_moments = _circular_block_moments(returns, block)
    partial_moments = _circular_block_moments(returns, remainder) if remainder else []
    sharpes: list[float] = []
    for _ in range(n_boot):
        total = 0.0
        total_sq = 0.0
        for _ in range(full_blocks):
            block_sum, block_sum_sq = full_moments[rng.randrange(n)]
            total += block_sum
            total_sq += block_sum_sq
        if remainder:
            block_sum, block_sum_sq = partial_moments[rng.randrange(n)]
            total += block_sum
            total_sq += block_sum_sq
        sharpes.append(
            _sharpe_from_moments(
                total,
                total_sq,
                n,
                periods_per_year=periods_per_year,
            )
        )
    sharpes.sort()
    lo = sharpes[max(0, int((alpha / 2.0) * n_boot))]
    hi = sharpes[min(n_boot - 1, int((1.0 - alpha / 2.0) * n_boot))]
    return (lo, hi)


def _circular_block_moments(
    returns: Sequence[float],
    block: int,
) -> list[tuple[float, float]]:
    n = len(returns)
    return [
        (
            sum(returns[(start + offset) % n] for offset in range(block)),
            sum(returns[(start + offset) % n] ** 2 for offset in range(block)),
        )
        for start in range(n)
    ]


def _sharpe_from_moments(
    total: float,
    total_sq: float,
    n: int,
    *,
    periods_per_year: int,
) -> float:
    if n < 2:
        return 0.0
    mu = total / n
    variance_num = total_sq - total * total / n
    if variance_num <= 0.0:
        return 0.0
    sd = math.sqrt(variance_num / (n - 1))
    return 0.0 if sd == 0.0 else (mu / sd) * math.sqrt(periods_per_year)


@dataclass(frozen=True)
class SignificanceReport:
    n_obs: int
    n_trials: int
    annual_sharpe: float
    per_period_sharpe: float
    skew: float
    excess_kurtosis: float
    psr_vs_zero: float  # P(true Sharpe > 0), ignoring multiple testing
    deflated_benchmark_sr: float  # per-period E[max] under the null
    deflated_sharpe: float  # DSR: P(true Sharpe > deflated benchmark)
    sharpe_ci_low: float
    sharpe_ci_high: float
    significant: bool
    verdict: str

    def summary(self) -> str:
        return "\n".join(
            [
                f"  observations: {self.n_obs}   strategies searched: {self.n_trials}",
                (
                    f"  annualized Sharpe: {self.annual_sharpe:+.3f}   "
                    f"(95% bootstrap CI {self.sharpe_ci_low:+.2f} .. "
                    f"{self.sharpe_ci_high:+.2f})"
                ),
                f"  skew {self.skew:+.2f}  excess-kurtosis {self.excess_kurtosis:+.2f}",
                f"  PSR vs 0 (ignores search): {self.psr_vs_zero:.3f}",
                (f"  deflated benchmark Sharpe (per-period): {self.deflated_benchmark_sr:.4f}"),
                (f"  DSR (P[edge | searched {self.n_trials}]): {self.deflated_sharpe:.3f}"),
                f"  VERDICT: {self.verdict}",
            ]
        )


def assess_edge(
    returns: Sequence[float],
    trial_sharpes: Sequence[float | None],
    *,
    alpha: float = 0.05,
    bootstrap_seed: int = 0,
    bootstrap_samples: int = 2000,
    periods_per_year: int = 252,
) -> SignificanceReport:
    """Full honest edge assessment for one strategy's (OOS) returns.

    ``returns`` are the selected strategy's realized (ideally out-of-sample)
    per-period returns. ``trial_sharpes`` are the per-period Sharpes of every
    configuration searched (use :func:`per_period_sharpe`); None/non-finite
    entries are dropped so a sharpe-less trial cannot NaN the whole assessment.
    """
    trial_sharpes = _finite_trial_sharpes(trial_sharpes)
    n = len(returns)
    annual = sharpe_ratio(returns, periods_per_year=periods_per_year)
    psr0 = probabilistic_sharpe_ratio(returns, 0.0)
    dsr, benchmark = deflated_sharpe_ratio(returns, trial_sharpes)
    ci_lo, ci_hi = block_bootstrap_sharpe_ci(
        returns,
        n_boot=bootstrap_samples,
        alpha=alpha,
        seed=bootstrap_seed,
        periods_per_year=periods_per_year,
    )
    significant = dsr >= (1.0 - alpha) and ci_lo > 0.0

    if n < 30:
        verdict = "INSUFFICIENT SAMPLE -- too few observations to judge an edge."
    elif significant:
        verdict = (
            f"EDGE -- Sharpe is significant at {int((1 - alpha) * 100)}% after "
            f"correcting for {len(trial_sharpes)} trials and non-normality."
        )
    elif dsr >= 0.90:
        verdict = (
            "MARGINAL -- suggestive but not significant after multiple-testing "
            "correction; needs more data or a stronger signal."
        )
    else:
        verdict = (
            "NO EDGE -- not significant after correcting for the search and fat "
            "tails. Most likely selection bias / luck, not skill."
        )

    return SignificanceReport(
        n_obs=n,
        n_trials=len(trial_sharpes),
        annual_sharpe=annual,
        per_period_sharpe=per_period_sharpe(returns),
        skew=skewness(returns),
        excess_kurtosis=excess_kurtosis(returns),
        psr_vs_zero=psr0,
        deflated_benchmark_sr=benchmark,
        deflated_sharpe=dsr,
        sharpe_ci_low=ci_lo,
        sharpe_ci_high=ci_hi,
        significant=significant,
        verdict=verdict,
    )
