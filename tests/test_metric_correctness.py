"""Exact regressions for accounting-period and published-model metric semantics."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_grader.metrics.fundamental import (
    altman_z,
    asset_turnover,
    book_value_cagr_5y,
    roa,
    roe,
    roic,
)
from stock_grader.metrics.models import altman_z_prime, beneish_m_score
from stock_grader.metrics.sector_specific import ffo_to_assets, net_interest_income_to_assets
from stock_grader.types import Fundamentals, SectorClass, SecuritySnapshot


def _snapshot(
    annual: pd.DataFrame,
    *,
    quarterly: pd.DataFrame | None = None,
    sic: str | None = None,
    sector: SectorClass = SectorClass.GENERAL,
) -> SecuritySnapshot:
    return SecuritySnapshot(
        ticker="FIXTURE",
        asof=date(2026, 3, 1),
        fundamentals=Fundamentals(
            quarterly=annual.copy() if quarterly is None else quarterly,
            annual=annual,
            filed=pd.Series(dtype="object"),
        ),
        sic=sic,
        sector=sector,
        price=10.0,
        shares_outstanding=100.0,
    )


class TestBookValuePerShareGrowth:
    def test_equity_growth_funded_by_matching_issuance_is_not_per_share_growth(self):
        index = pd.to_datetime([f"{year}-12-31" for year in range(2020, 2026)])
        shares = np.linspace(10.0, 20.0, len(index))
        annual = pd.DataFrame(
            {
                "equity": shares * 10.0,
                "shares_diluted": shares,
                # Assets move with shares, identifying genuine issuance rather than a split.
                "assets": shares * 30.0,
            },
            index=index,
        )
        assert book_value_cagr_5y.fn(_snapshot(annual)) == pytest.approx(0.0, abs=1e-12)

    def test_split_seam_is_rebased_before_per_share_growth(self):
        index = pd.to_datetime([f"{year}-12-31" for year in range(2020, 2026)])
        annual = pd.DataFrame(
            {
                "equity": [100.0] * 6,
                "shares_diluted": [10.0, 10.0, 10.0, 100.0, 100.0, 100.0],
                # A split changes the count while the business scale remains ordinary.
                "assets": [500.0, 510.0, 520.0, 530.0, 540.0, 550.0],
            },
            index=index,
        )
        assert book_value_cagr_5y.fn(_snapshot(annual)) == pytest.approx(0.0, abs=1e-12)

    def test_missing_aligned_share_year_is_not_replaced_with_total_equity_growth(self):
        index = pd.to_datetime([f"{year}-12-31" for year in range(2020, 2026)])
        annual = pd.DataFrame(
            {
                "equity": np.linspace(100.0, 200.0, 6),
                "shares_diluted": [10.0, 11.0, np.nan, 13.0, 14.0, 15.0],
                "assets": np.linspace(500.0, 600.0, 6),
            },
            index=index,
        )
        assert book_value_cagr_5y.fn(_snapshot(annual)) is None


class TestAverageBalanceReturns:
    def _two_year_frame(self) -> pd.DataFrame:
        index = pd.to_datetime(["2024-12-31", "2025-12-31"])
        return pd.DataFrame(
            {
                "net_income": [20.0, 30.0],
                "revenue": [700.0, 800.0],
                "ebit": [40.0, 50.0],
                "income_tax": [8.0, 10.0],
                "pretax_income": [40.0, 50.0],
                "equity": [100.0, 200.0],
                "assets": [300.0, 500.0],
                "invested_capital": [100.0, 300.0],
            },
            index=index,
        )

    def test_roe_roa_roic_and_turnover_use_beginning_ending_average(self):
        snapshot = _snapshot(self._two_year_frame())
        assert roe.fn(snapshot) == pytest.approx(30.0 / 150.0)
        assert roa.fn(snapshot) == pytest.approx(30.0 / 400.0)
        assert roic.fn(snapshot) == pytest.approx((50.0 * 0.8) / 200.0)
        assert asset_turnover.fn(snapshot) == pytest.approx(800.0 / 400.0)

    def test_single_ending_balance_preserves_coverage_without_inventing_a_beginning(self):
        annual = self._two_year_frame().iloc[-1:]
        snapshot = _snapshot(annual)
        assert roa.fn(snapshot) == pytest.approx(30.0 / 500.0)

    def test_nonpositive_aligned_beginning_balance_is_not_ignored(self):
        annual = self._two_year_frame()
        annual.loc[annual.index[0], "equity"] = -10.0
        assert roe.fn(_snapshot(annual)) is None

    def test_bank_and_reit_asset_returns_use_the_same_average_contract(self):
        annual = self._two_year_frame()
        annual["net_interest_income"] = [80.0, 100.0]
        annual["income_to_common"] = [90.0, 100.0]
        annual["depreciation_amortization"] = [40.0, 50.0]
        bank = _snapshot(annual, sic="6021", sector=SectorClass.BANK)
        reit = _snapshot(annual, sic="6798", sector=SectorClass.REIT)

        assert net_interest_income_to_assets.fn(bank) == pytest.approx(100.0 / 400.0)
        ffo_result = ffo_to_assets.fn(reit)
        assert ffo_result is not None
        assert ffo_result[0] == pytest.approx(150.0 / 400.0)


def _stable_beneish_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2024-12-31", "2025-12-31"])
    return pd.DataFrame(
        {
            "revenue": [1000.0, 1000.0],
            "receivables": [100.0, 100.0],
            "gross_profit": [400.0, 400.0],
            "assets": [1000.0, 1000.0],
            "current_assets": [300.0, 300.0],
            "ppe_net": [500.0, 500.0],
            "depreciation_amortization": [50.0, 50.0],
            "sganda_expense": [100.0, 100.0],
            "long_term_debt": [200.0, 200.0],
            "current_liabilities": [100.0, 100.0],
            "net_income": [100.0, 100.0],
            "cfo": [100.0, 100.0],
        },
        index=index,
    )


class TestBeneishPeriodIntegrity:
    def test_complete_same_year_inputs_reproduce_the_published_equation(self):
        result = beneish_m_score.fn(_snapshot(_stable_beneish_frame()))
        assert result is not None
        score, inputs = result
        assert score == pytest.approx(-2.48)
        assert inputs["n_components"] == 8.0
        assert inputs["n_substituted"] == 0.0

    def test_independently_available_but_misaligned_years_are_rejected(self):
        frame = _stable_beneish_frame()
        earlier = frame.iloc[[0]].copy()
        earlier.index = pd.to_datetime(["2023-12-31"])
        frame = pd.concat([earlier, frame])
        frame.loc[pd.Timestamp("2024-12-31"), "receivables"] = np.nan
        # The old per-column pair silently compared 2023 receivables with 2025 while every other
        # component compared 2024 with 2025.
        assert beneish_m_score.fn(_snapshot(frame)) is None

    def test_extreme_component_is_quarantined_not_neutral_imputed(self):
        frame = _stable_beneish_frame()
        frame.loc[frame.index[-1], "receivables"] = 1000.0
        assert beneish_m_score.fn(_snapshot(frame)) is None

    def test_missing_warning_component_is_not_neutral_imputed(self):
        frame = _stable_beneish_frame().drop(columns=["long_term_debt"])
        assert beneish_m_score.fn(_snapshot(frame)) is None


def _altman_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2024-12-31", "2025-12-31"])
    return pd.DataFrame(
        {
            "assets": [900.0, 1000.0],
            "liabilities": [500.0, 600.0],
            "working_capital": [180.0, 220.0],
            "retained_earnings": [100.0, 120.0],
            "equity": [400.0, 400.0],
            "ebit": [80.0, 90.0],
            "revenue": [1000.0, 1100.0],
        },
        index=index,
    )


class TestAltmanVariantExclusivity:
    def test_public_manufacturer_gets_only_original_z(self):
        snapshot = _snapshot(_altman_frame(), sic="3571")
        assert altman_z.fn(snapshot) is not None
        assert altman_z_prime.fn(snapshot) is None

    def test_nonmanufacturer_gets_only_z_double_prime(self):
        snapshot = _snapshot(_altman_frame(), sic="5331")
        assert altman_z.fn(snapshot) is None
        assert altman_z_prime.fn(snapshot) is not None

    @pytest.mark.parametrize(
        "sector,sic",
        [
            (SectorClass.BANK, "6021"),
            (SectorClass.INSURANCE, "6311"),
            (SectorClass.REIT, "6798"),
            (SectorClass.HOLDING, "6719"),
            (SectorClass.UTILITY, "4911"),
        ],
    )
    def test_unsupported_business_models_get_neither_variant(self, sector, sic):
        snapshot = _snapshot(_altman_frame(), sic=sic, sector=sector)
        assert altman_z.fn(snapshot) is None
        assert altman_z_prime.fn(snapshot) is None

    def test_missing_sic_does_not_guess_and_double_count(self):
        snapshot = _snapshot(_altman_frame(), sic=None)
        assert altman_z.fn(snapshot) is None
        assert altman_z_prime.fn(snapshot) is None
