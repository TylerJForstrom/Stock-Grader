"""§2 scoring-structure regressions: redundancy groups, the risk-pillar split,
and the stability quarantine."""

from __future__ import annotations

import pandas as pd
import pytest

from stock_grader.pipeline import _apply_redundancy_groups, _pillar_members
from stock_grader.profiles import get_profile
from stock_grader.registry import METRICS


class TestRedundancyGroups:
    def test_groups_share_one_slot_each(self):
        weights = pd.Series(
            {
                "pe_trailing": 0.2, "earnings_yield": 0.2,       # one group
                "price_to_fcf": 0.2, "ev_to_fcf": 0.2, "fcf_yield": 0.2,  # one group
            }
        )
        out = _apply_redundancy_groups(weights)
        assert out.sum() == pytest.approx(1.0)
        assert out["pe_trailing"] + out["earnings_yield"] == pytest.approx(0.5)
        assert out[["price_to_fcf", "ev_to_fcf", "fcf_yield"]].sum() == pytest.approx(0.5)

    def test_singletons_untouched(self):
        weights = pd.Series({"roe": 0.5, "roa": 0.5})
        out = _apply_redundancy_groups(weights)
        pd.testing.assert_series_equal(out, weights)

    def test_reciprocal_pairs_are_registered_as_groups(self):
        assert METRICS.get("pe_trailing").group == METRICS.get("earnings_yield").group
        assert (
            METRICS.get("price_to_fcf").group
            == METRICS.get("ev_to_fcf").group
            == METRICS.get("fcf_yield").group
        )
        assert METRICS.get("altman_z").group == METRICS.get("altman_z_prime").group

    def test_momentum_family_is_one_group(self):
        family = [
            "momentum_3m", "momentum_6m", "momentum_12_1", "risk_adjusted_momentum",
            "momentum_consistency", "pct_positive_days", "distance_from_52w_high",
            "price_to_sma200", "golden_cross", "trend_strength",
        ]
        groups = {METRICS.get(name).group for name in family}
        assert groups == {"trailing_momentum"}


class TestRiskPillarSplit:
    def test_risk_adjusted_performance_left_the_risk_pillar(self):
        for name in ("sharpe_ratio", "sortino_ratio", "calmar_ratio", "capm_alpha",
                     "annualized_return_1y"):
            assert METRICS.get(name).pillar == "risk_adjusted_return", name

    def test_pure_risk_stays(self):
        for name in ("annualized_volatility", "max_drawdown", "var_95"):
            assert METRICS.get(name).pillar == "risk", name

    def test_time_series_diagnostics_quarantined(self):
        for name in ("hurst_exponent", "variance_ratio", "return_autocorrelation",
                     "mean_reversion_half_life"):
            assert METRICS.get(name).pillar == "stability", name

    def test_low_volatility_profile_buys_pure_risk(self):
        config = get_profile("low_volatility")
        assert config.pillar_weights["risk"] == pytest.approx(0.30)
        assert config.pillar_weights["risk_adjusted_return"] == pytest.approx(0.10)

    def test_pillar_members_reflect_new_taxonomy(self):
        members = _pillar_members(["sharpe_ratio", "annualized_volatility", "hurst_exponent"])
        assert members == {
            "risk_adjusted_return": ["sharpe_ratio"],
            "risk": ["annualized_volatility"],
            "stability": ["hurst_exponent"],
        }
