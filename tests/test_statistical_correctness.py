"""Deterministic methodology regressions for market, risk, and momentum metrics."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_grader.metrics.statistical import (
    TRADING_DAYS,
    _daily_risk_free,
    annualized_return_1y,
    beta,
    capm_alpha,
    cvar_95,
    idiosyncratic_volatility,
    information_discreteness,
    max_drawdown,
    mean_reversion_half_life,
    momentum_12_1,
    risk_adjusted_momentum,
    sharpe_ratio,
    sortino_ratio,
    var_95,
)
from stock_grader.types import SecuritySnapshot


def _price_frame(
    log_returns: np.ndarray | list[float],
    *,
    start: str = "2022-01-03",
) -> pd.DataFrame:
    returns = np.asarray(log_returns, dtype="float64")
    index = pd.bdate_range(start, periods=len(returns) + 1)
    levels = 100.0 * np.exp(np.r_[0.0, np.cumsum(returns)])
    return pd.DataFrame({"close": levels, "adj_close": levels}, index=index)


def _snapshot(
    log_returns: np.ndarray | list[float],
    *,
    benchmark_returns: np.ndarray | list[float] | None = None,
    risk_free: pd.Series | None = None,
) -> SecuritySnapshot:
    prices = _price_frame(log_returns)
    benchmark = _price_frame(benchmark_returns) if benchmark_returns is not None else None
    return SecuritySnapshot(
        ticker="FIXTURE",
        asof=date(2026, 1, 31),
        prices=prices,
        benchmark=benchmark,
        risk_free=risk_free,
    )


class TestRiskFreeAndAnnualization:
    def test_risk_free_alignment_never_backfills_future_observations(self):
        target = pd.bdate_range("2025-01-01", periods=6)
        risk_free = pd.Series(0.05, index=target[2:4])
        snapshot = SecuritySnapshot(
            ticker="RF",
            asof=date(2025, 1, 31),
            prices=_price_frame(np.zeros(10)),
            risk_free=risk_free,
        )
        daily = _daily_risk_free(snapshot, target)

        assert daily.iloc[:2].isna().all()
        assert daily.iloc[2] == pytest.approx(np.log1p(0.05) / TRADING_DAYS)

    def test_stale_risk_free_observation_is_not_forward_filled_indefinitely(self):
        target = pd.to_datetime(["2025-01-02", "2025-01-10", "2025-01-20"])
        snapshot = SecuritySnapshot(
            ticker="RF",
            asof=date(2025, 1, 31),
            prices=_price_frame(np.zeros(10)),
            risk_free=pd.Series([0.04], index=pd.to_datetime(["2025-01-02"])),
        )
        daily = _daily_risk_free(snapshot, target)

        assert np.isfinite(daily.iloc[0])
        assert np.isfinite(daily.iloc[1])
        assert np.isnan(daily.iloc[2])

    def test_one_year_metrics_require_252_return_observations_not_252_prices(self):
        too_short = _snapshot(np.full(251, 0.001))
        enough = _snapshot(np.full(252, 0.001))
        assert annualized_return_1y.fn(too_short) is None
        assert annualized_return_1y.fn(enough) == pytest.approx(0.252)

    def test_recent_material_price_gap_restarts_the_usable_return_history(self):
        frame = _price_frame(np.full(302, 0.001))
        shifted = frame.index.to_series()
        shifted.iloc[252:] = shifted.iloc[252:] + pd.Timedelta(days=14)
        frame.index = pd.DatetimeIndex(shifted)
        snapshot = SecuritySnapshot(
            ticker="GAPPED",
            asof=date(2026, 1, 31),
            prices=frame,
        )

        # More than a year of observations exists in total, but only 50 clean post-gap returns.
        # Silently joining both sides would omit the gap return and understate risk.
        assert annualized_return_1y.fn(snapshot) is None

    def test_sharpe_and_sortino_use_equivalent_daily_log_risk_free_rate(self):
        returns = np.resize(np.array([0.012, -0.008, 0.006, -0.003]), TRADING_DAYS)
        prices = _price_frame(returns)
        annual_rf = 0.04
        risk_free = pd.Series(annual_rf, index=prices.index)
        snapshot = SecuritySnapshot(
            ticker="RATIOS",
            asof=date(2026, 1, 31),
            prices=prices,
            risk_free=risk_free,
        )
        daily_rf = np.log1p(annual_rf) / TRADING_DAYS
        excess = returns - daily_rf
        expected_sharpe = excess.mean() / excess.std(ddof=1) * np.sqrt(TRADING_DAYS)
        downside = np.sqrt(np.square(excess[excess < 0]).sum() / len(excess))
        expected_sortino = excess.mean() / downside * np.sqrt(TRADING_DAYS)

        assert sharpe_ratio.fn(snapshot) == pytest.approx(expected_sharpe)
        assert sortino_ratio.fn(snapshot) == pytest.approx(expected_sortino)

    def test_direct_risk_adjusted_call_without_rate_is_missing_unless_explicit(self):
        returns = np.resize(np.array([0.01, -0.005]), TRADING_DAYS)
        snapshot = _snapshot(returns)
        assert sharpe_ratio.fn(snapshot) is None
        snapshot.meta["assume_zero_risk_free"] = True
        assert sharpe_ratio.fn(snapshot) is not None


class TestHistoricalTailRisk:
    def test_var_and_expected_shortfall_use_a_full_year_and_exact_tail_count(self):
        returns = np.linspace(-0.05, 0.05, TRADING_DAYS)
        snapshot = _snapshot(returns)
        expected_var = max(0.0, -float(np.percentile(returns, 5)))
        tail_count = int(np.ceil(0.05 * len(returns)))
        expected_shortfall = max(0.0, -float(np.sort(returns)[:tail_count].mean()))

        assert var_95.fn(snapshot) == pytest.approx(expected_var)
        assert cvar_95.fn(snapshot) == pytest.approx(expected_shortfall)
        assert cvar_95.fn(snapshot) >= var_95.fn(snapshot)

    def test_all_positive_tail_is_zero_loss_not_negative_var(self):
        snapshot = _snapshot(np.linspace(0.001, 0.01, TRADING_DAYS))
        assert var_95.fn(snapshot) == pytest.approx(0.0)
        assert cvar_95.fn(snapshot) == pytest.approx(0.0)

    def test_three_year_drawdown_requires_three_years_of_returns(self):
        assert max_drawdown.fn(_snapshot(np.zeros(TRADING_DAYS * 3 - 1))) is None
        assert max_drawdown.fn(_snapshot(np.zeros(TRADING_DAYS * 3))) == pytest.approx(0.0)


class TestFactorAlignment:
    def _factor_snapshot(self) -> tuple[SecuritySnapshot, float, float]:
        n = TRADING_DAYS * 2
        x = np.arange(n, dtype="float64")
        market = 0.0003 + 0.008 * np.sin(x / 9.0) + 0.003 * np.cos(x / 17.0)
        slope = 1.4
        daily_alpha = 0.0002
        security = daily_alpha + slope * market
        snapshot = _snapshot(security, benchmark_returns=market)
        snapshot.risk_free = pd.Series(0.0, index=snapshot.prices.index)
        return snapshot, slope, daily_alpha

    def test_beta_and_alpha_use_exactly_common_daily_intervals(self):
        snapshot, expected_beta, daily_alpha = self._factor_snapshot()
        # Removing benchmark dates used to pair the next two-day market return with a one-day
        # security return. The corrected implementation discards those non-comparable intervals.
        snapshot.benchmark = snapshot.benchmark.drop(
            snapshot.benchmark.index[[100, 300]]
        )

        assert beta.fn(snapshot) == pytest.approx(expected_beta, abs=1e-10)
        assert capm_alpha.fn(snapshot) == pytest.approx(
            daily_alpha * TRADING_DAYS,
            abs=1e-10,
        )
        assert idiosyncratic_volatility.fn(snapshot) == pytest.approx(0.0, abs=1e-10)

    def test_sparse_benchmark_does_not_support_a_factor_regression(self):
        snapshot, _expected_beta, _daily_alpha = self._factor_snapshot()
        snapshot.benchmark = snapshot.benchmark.iloc[::2]
        assert beta.fn(snapshot) is None
        assert capm_alpha.fn(snapshot) is None


class TestMeanReversionSemantics:
    def test_stationary_log_price_uses_exact_discrete_half_life_and_lower_is_better(self):
        rng = np.random.default_rng(7)
        phi = 0.90
        log_price = np.zeros(TRADING_DAYS * 3)
        for i in range(1, len(log_price)):
            log_price[i] = phi * log_price[i - 1] + rng.normal(0.0, 0.01)
        levels = 100.0 * np.exp(log_price)
        frame = pd.DataFrame(
            {"close": levels, "adj_close": levels},
            index=pd.bdate_range("2022-01-03", periods=len(levels)),
        )
        snapshot = SecuritySnapshot(
            ticker="OU",
            asof=date(2026, 1, 31),
            prices=frame,
        )
        estimated_phi = np.cov(log_price[1:], log_price[:-1], ddof=1)[0, 1] / np.var(
            log_price[:-1],
            ddof=1,
        )
        expected = -np.log(2.0) / np.log(estimated_phi)

        assert mean_reversion_half_life.direction == -1
        assert mean_reversion_half_life.fn(snapshot) == pytest.approx(expected)

    def test_unit_root_price_is_quarantined_instead_of_given_a_spurious_half_life(self):
        rng = np.random.default_rng(123)
        log_price = np.cumsum(rng.normal(0.0002, 0.01, TRADING_DAYS * 3))
        levels = 100.0 * np.exp(log_price)
        frame = pd.DataFrame(
            {"close": levels, "adj_close": levels},
            index=pd.bdate_range("2022-01-03", periods=len(levels)),
        )
        snapshot = SecuritySnapshot(
            ticker="RANDOM_WALK",
            asof=date(2026, 1, 31),
            prices=frame,
        )
        assert mean_reversion_half_life.fn(snapshot) is None


class TestMomentumWindowIntegrity:
    def test_risk_and_information_use_the_same_twelve_to_one_formation_window(self):
        formation = np.resize(np.array([0.004, 0.004, -0.001]), 231)
        recent_month = np.resize(np.array([0.10, -0.10, 0.08]), 21)
        returns = np.r_[formation, recent_month]
        snapshot = _snapshot(returns)

        expected_momentum = float(np.expm1(formation.sum()))
        expected_volatility = float(formation.std(ddof=1) * np.sqrt(TRADING_DAYS))
        expected_discreteness = float(
            np.sign(expected_momentum)
            * ((formation < 0).mean() - (formation > 0).mean())
        )

        assert momentum_12_1.fn(snapshot) == pytest.approx(expected_momentum)
        assert risk_adjusted_momentum.fn(snapshot) == pytest.approx(
            expected_momentum / expected_volatility
        )
        assert information_discreteness.fn(snapshot) == pytest.approx(expected_discreteness)
