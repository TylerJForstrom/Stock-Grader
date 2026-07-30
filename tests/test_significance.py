"""Edge-significance statistics: PSR, DSR, multiple-testing correction."""

from __future__ import annotations

import math
import random

from stock_grader.significance import (
    SignificanceReport,
    assess_edge,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    norm_cdf,
    norm_ppf,
    per_period_sharpe,
    probabilistic_sharpe_ratio,
    skewness,
)

# --- normal helpers ---------------------------------------------------------


def test_norm_cdf_known_values() -> None:
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(norm_cdf(-1.96) - 0.025) < 1e-3


def test_norm_ppf_inverts_cdf() -> None:
    for p in (0.01, 0.1, 0.5, 0.9, 0.975, 0.999):
        assert abs(norm_cdf(norm_ppf(p)) - p) < 1e-6


def test_norm_ppf_known_reference_values() -> None:
    # Standard normal quantiles from statistical tables / scipy.stats.norm.ppf.
    refs = {
        0.001: -3.090232306,
        0.025: -1.959963985,
        0.5: 0.0,
        0.975: 1.959963985,
        0.999: 3.090232306,
    }
    for p, expected in refs.items():
        assert abs(norm_ppf(p) - expected) < 1e-6


def test_norm_ppf_edges() -> None:
    assert norm_ppf(0.0) == float("-inf")
    assert norm_ppf(1.0) == float("inf")


def test_skewness_symmetric_is_zero() -> None:
    xs = [-2.0, -1.0, 0.0, 1.0, 2.0]
    assert abs(skewness(xs)) < 1e-12


# --- PSR --------------------------------------------------------------------


def test_psr_at_observed_sharpe_is_half() -> None:
    # Benchmark == observed per-period Sharpe => PSR = Phi(0) = 0.5.
    rng = random.Random(0)
    returns = [rng.gauss(0.001, 0.01) for _ in range(500)]
    sr = per_period_sharpe(returns)
    assert abs(probabilistic_sharpe_ratio(returns, sr) - 0.5) < 1e-9


def test_psr_grows_with_sample_size() -> None:
    rng = random.Random(1)
    short = [rng.gauss(0.0008, 0.01) for _ in range(60)]
    long = [rng.gauss(0.0008, 0.01) for _ in range(4000)]
    assert probabilistic_sharpe_ratio(long, 0.0) > probabilistic_sharpe_ratio(
        short, 0.0
    )


def test_psr_extreme_kurtosis_positive_signal_is_not_pinned_to_zero() -> None:
    # Excess kurtosis near 200, comparable to the real 5m tail concern. The
    # denominator stays positive and PSR can still recognize a genuine signal.
    returns = [0.0001] * 9950 + [0.1] * 25 + [-0.1] * 25
    assert probabilistic_sharpe_ratio(returns, 0.0) > 0.9


# --- DSR / multiple testing -------------------------------------------------


def test_expected_max_sharpe_increases_with_trials() -> None:
    e10 = expected_max_sharpe(0.05, 10)
    e1000 = expected_max_sharpe(0.05, 1000)
    assert e1000 > e10 > 0.0


def test_more_trials_lowers_dsr() -> None:
    rng = random.Random(2)
    returns = [rng.gauss(0.0010, 0.01) for _ in range(2000)]
    trial_sharpes = [rng.gauss(0.0, 0.03) for _ in range(50)]
    dsr_few, _ = deflated_sharpe_ratio(returns, trial_sharpes[:3])
    dsr_many, _ = deflated_sharpe_ratio(returns, trial_sharpes)
    assert dsr_many < dsr_few


def test_dsr_extreme_kurtosis_strong_signal_can_still_pass() -> None:
    # If high kurtosis mechanically forced DSR to zero, this would fail. A
    # strong enough fat-tailed signal still clears the deflated benchmark.
    returns = [0.0002] * 9990 + [0.2] * 5 + [-0.2] * 5
    dsr, benchmark = deflated_sharpe_ratio(returns, [0.0, 0.005, 0.010, 0.015, 0.020])
    assert benchmark > 0.0
    assert dsr > 0.95


def test_strong_signal_few_trials_is_significant() -> None:
    rng = random.Random(3)
    # A genuinely strong, long, near-normal track record.
    returns = [rng.gauss(0.0015, 0.008) for _ in range(4000)]
    trials = [rng.gauss(0.0, 0.01) for _ in range(5)]
    report = assess_edge(returns, trials)
    assert report.significant
    assert report.deflated_sharpe > 0.95
    assert "EDGE" in report.verdict


def test_weak_signal_many_trials_is_no_edge() -> None:
    rng = random.Random(4)
    # Tiny drift, short sample, and a big search => luck, not skill.
    returns = [rng.gauss(0.0002, 0.012) for _ in range(300)]
    trials = [rng.gauss(0.0, 0.05) for _ in range(200)]
    report = assess_edge(returns, trials)
    assert not report.significant
    assert "NO EDGE" in report.verdict or "MARGINAL" in report.verdict


def test_deflation_ignores_none_and_non_finite_trial_sharpes() -> None:
    # A ledger trial whose Sharpe could not be computed arrives as None (or a
    # legacy NaN). One such value must not turn stdev — and every later DSR —
    # into NaN.
    rng = random.Random(6)
    returns = [rng.gauss(0.001, 0.01) for _ in range(200)]
    clean_trials = [0.01, 0.02, 0.03]
    polluted_trials = [0.01, None, float("nan"), 0.02, float("inf"), 0.03]

    clean = assess_edge(returns, clean_trials, bootstrap_samples=100)
    polluted = assess_edge(returns, polluted_trials, bootstrap_samples=100)

    assert polluted.n_trials == clean.n_trials == 3
    assert polluted.deflated_sharpe == clean.deflated_sharpe
    assert math.isfinite(polluted.deflated_sharpe)

    dsr, benchmark = deflated_sharpe_ratio(returns, polluted_trials)
    assert (dsr, benchmark) == deflated_sharpe_ratio(returns, clean_trials)


def test_assess_edge_reports_all_fields() -> None:
    rng = random.Random(5)
    returns = [rng.gauss(0.001, 0.01) for _ in range(500)]
    report = assess_edge(returns, [0.01, 0.02, 0.03])
    assert isinstance(report, SignificanceReport)
    assert report.n_obs == 500
    assert report.n_trials == 3
    assert 0.0 <= report.deflated_sharpe <= 1.0
    assert report.sharpe_ci_low <= report.sharpe_ci_high
