"""Exact regressions for accounting-period and published-model metric semantics."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from stock_grader.metrics.engine import evaluate_metrics, evaluate_one
from stock_grader.metrics.fundamental import (
    altman_z,
    asset_turnover,
    book_value_cagr_5y,
    graham_number_ratio,
    piotroski_f_score,
    roa,
    roe,
    roic,
)
from stock_grader.metrics.models import altman_z_prime, beneish_m_score, ohlson_o_score
from stock_grader.metrics.sector_specific import ffo_to_assets, net_interest_income_to_assets
from stock_grader.registry import METRICS
from stock_grader.types import Coverage, Fundamentals, SectorClass, SecuritySnapshot


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


def test_public_float_lower_bound_is_never_an_exact_valuation_input():
    annual = pd.DataFrame(
        {
            "net_income": [100.0],
            "equity": [1_000.0],
        },
        index=pd.to_datetime(["2025-12-31"]),
    )
    snapshot = _snapshot(annual)
    snapshot.meta["price_source"] = "public_float_lower_bound"
    snapshot.meta["price_lower_bound"] = 10.0
    snapshot.meta["valuation_price_rejected"] = "public_float_lower_bound"

    assert snapshot.valuation_price is None
    assert snapshot.market_cap is None
    assert graham_number_ratio.fn(snapshot) is None
    result = evaluate_one(METRICS.get("pe_trailing"), snapshot)
    assert result.coverage is Coverage.MISSING
    assert "only a lower bound" in result.note
    assert result.raw_inputs["price_lower_bound"] == pytest.approx(10.0)
    valuation_names = [name for name, spec in METRICS.items() if spec.pillar == "valuation"]
    valuation_results = evaluate_metrics(snapshot, names=valuation_names)
    assert not any(item.coverage is Coverage.OK for item in valuation_results.values())
    for item in valuation_results.values():
        if item.coverage is Coverage.MISSING:
            assert "only a lower bound" in item.note
            assert item.raw_inputs["valuation_price_rejected"] == "public_float_lower_bound"


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

    def test_future_flow_row_cannot_move_the_selected_period_end(self):
        quarterly = pd.DataFrame(
            {
                "net_income": [np.nan, 10.0, 10.0, 10.0, 10.0, 999.0],
                "assets": [100.0, 150.0, 200.0, 250.0, 300.0, 10_000.0],
            },
            index=pd.to_datetime(
                [
                    "2024-12-31",
                    "2025-03-31",
                    "2025-06-30",
                    "2025-09-30",
                    "2025-12-31",
                    "2026-06-30",
                ]
            ),
        )
        annual = pd.DataFrame(
            {"net_income": [20.0, 40.0], "assets": [100.0, 300.0]},
            index=pd.to_datetime(["2024-12-31", "2025-12-31"]),
        )

        assert roa.fn(_snapshot(annual, quarterly=quarterly)) == pytest.approx(40.0 / 200.0)


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

    def test_prior_net_income_and_cfo_are_not_invented_requirements(self):
        frame = _stable_beneish_frame()
        expected = beneish_m_score.fn(_snapshot(frame))
        frame.loc[frame.index[0], ["net_income", "cfo"]] = np.nan
        assert beneish_m_score.fn(_snapshot(frame)) == expected

    def test_stale_or_nonconsecutive_fiscal_pairs_are_rejected(self):
        stale = _stable_beneish_frame()
        stale.index = pd.to_datetime(["2019-12-31", "2020-12-31"])
        assert beneish_m_score.fn(_snapshot(stale)) is None

        gapped = _stable_beneish_frame()
        gapped.index = pd.to_datetime(["2023-12-31", "2025-12-31"])
        assert beneish_m_score.fn(_snapshot(gapped)) is None

    def test_future_fiscal_rows_are_not_visible(self):
        future = _stable_beneish_frame()
        future.index = pd.to_datetime(["2026-12-31", "2027-12-31"])
        assert beneish_m_score.fn(_snapshot(future)) is None


def _ohlson_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "assets": [900.0, 1_000.0],
            "liabilities": [500.0, 600.0],
            "working_capital": [180.0, 200.0],
            "current_liabilities": [220.0, 250.0],
            "current_assets": [400.0, 500.0],
            "net_income": [50.0, 80.0],
            "cfo": [70.0, 100.0],
        },
        index=pd.to_datetime(["2024-12-31", "2025-12-31"]),
    )


class TestOhlsonPeriodIntegrity:
    def test_complete_aligned_inputs_reproduce_the_declared_equation(self):
        result = ohlson_o_score.fn(_snapshot(_ohlson_frame()))
        assert result is not None
        score, raw = result
        expected = (
            -1.32
            - 0.407 * np.log(1_000.0 / 100.0)
            + 6.03 * (600.0 / 1_000.0)
            - 1.43 * (200.0 / 1_000.0)
            + 0.0757 * (250.0 / 500.0)
            - 2.37 * (80.0 / 1_000.0)
            - 1.83 * (100.0 / 600.0)
            - 0.521 * ((80.0 - 50.0) / (80.0 + 50.0))
        )
        assert score == pytest.approx(expected)
        assert raw["funds_from_operations_proxy"] == "cash_from_operations"

    @pytest.mark.parametrize("missing", list(_ohlson_frame().columns))
    def test_every_declared_component_is_required(self, missing):
        assert ohlson_o_score.fn(_snapshot(_ohlson_frame().drop(columns=[missing]))) is None

    def test_only_prior_net_income_is_required_from_the_prior_year(self):
        frame = _ohlson_frame()
        expected = ohlson_o_score.fn(_snapshot(frame))
        frame.loc[frame.index[0], frame.columns.difference(["net_income"])] = np.nan
        assert ohlson_o_score.fn(_snapshot(frame)) == expected

    def test_stale_and_gapped_pairs_are_rejected(self):
        stale = _ohlson_frame()
        stale.index = pd.to_datetime(["2019-12-31", "2020-12-31"])
        assert ohlson_o_score.fn(_snapshot(stale)) is None

        gapped = _ohlson_frame()
        gapped.index = pd.to_datetime(["2023-12-31", "2025-12-31"])
        assert ohlson_o_score.fn(_snapshot(gapped)) is None


def _piotroski_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "assets": [1_000.0, 1_000.0, 1_000.0],
            "net_income": [np.nan, 50.0, 100.0],
            "cfo": [np.nan, np.nan, 150.0],
            "long_term_debt": [np.nan, 300.0, 200.0],
            "current_assets": [np.nan, 400.0, 500.0],
            "current_liabilities": [np.nan, 200.0, 180.0],
            "shares_diluted": [np.nan, 100.0, 99.0],
            "gross_profit": [np.nan, 300.0, 400.0],
            "revenue": [np.nan, 800.0, 900.0],
        },
        index=pd.to_datetime(["2023-12-31", "2024-12-31", "2025-12-31"]),
    )


class TestPiotroskiCanonicalContract:
    def test_requires_all_nine_components_and_returns_integer_score(self):
        frame = _piotroski_frame()
        result = piotroski_f_score.fn(_snapshot(frame))
        assert result is not None
        score, raw = result
        assert score == 9.0
        assert raw["n_components"] == 9

        assert piotroski_f_score.fn(_snapshot(frame.drop(columns=["long_term_debt"]))) is None

    def test_stale_and_nonconsecutive_pairs_are_rejected(self):
        frame = _piotroski_frame()
        frame.index = pd.to_datetime(["2018-12-31", "2019-12-31", "2020-12-31"])
        assert piotroski_f_score.fn(_snapshot(frame)) is None
        frame.index = pd.to_datetime(["2022-12-31", "2023-12-31", "2025-12-31"])
        assert piotroski_f_score.fn(_snapshot(frame)) is None

    def test_published_asset_denominators_drive_change_signals(self):
        frame = _piotroski_frame()
        frame["assets"] = [100.0, 1_000.0, 100.0]
        frame["net_income"] = [np.nan, 10.0, 20.0]
        frame["long_term_debt"] = [np.nan, 100.0, 50.0]
        frame["revenue"] = [np.nan, 100.0, 200.0]
        frame["gross_profit"] = [np.nan, 40.0, 80.0]
        result = piotroski_f_score.fn(_snapshot(frame))
        assert result is not None
        _score, raw = result
        assert raw["rising_roa"] == 0
        assert raw["rising_asset_turnover"] == 0
        assert raw["falling_leverage"] == 1


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

    def test_statement_inputs_must_share_one_fiscal_period(self):
        frame = _altman_frame().iloc[[1]].copy()
        earlier = frame.copy()
        earlier.index = pd.to_datetime(["2025-03-31"])
        earlier[["assets", "liabilities", "working_capital", "retained_earnings", "equity"]] = (
            np.nan
        )
        frame[["ebit", "revenue"]] = np.nan
        mixed = pd.concat([earlier, frame]).sort_index()

        assert altman_z.fn(_snapshot(mixed, sic="3571")) is None
        assert altman_z_prime.fn(_snapshot(mixed, sic="5331")) is None

    def test_hand_calculated_variants_use_the_same_annual_row(self):
        frame = _altman_frame()
        manufacturer = _snapshot(frame, sic="3571")
        retailer = _snapshot(frame, sic="5331")

        z = altman_z.fn(manufacturer)
        assert z == pytest.approx(
            1.2 * 220.0 / 1_000.0
            + 1.4 * 120.0 / 1_000.0
            + 3.3 * 90.0 / 1_000.0
            + 0.6 * manufacturer.market_cap / 600.0
            + 1_100.0 / 1_000.0
        )
        z_prime = altman_z_prime.fn(retailer)
        assert z_prime is not None
        score, _raw = z_prime
        assert score == pytest.approx(
            6.56 * 220.0 / 1_000.0
            + 3.26 * 120.0 / 1_000.0
            + 6.72 * 90.0 / 1_000.0
            + 1.05 * 400.0 / 600.0
        )

    def test_inactive_variant_is_not_applicable_not_missing(self):
        manufacturer = _snapshot(_altman_frame(), sic="3571")
        retailer = _snapshot(_altman_frame(), sic="5331")

        assert evaluate_one(METRICS.get("altman_z"), manufacturer).coverage is Coverage.OK
        assert (
            evaluate_one(METRICS.get("altman_z_prime"), manufacturer).coverage
            is Coverage.NOT_APPLICABLE
        )
        assert evaluate_one(METRICS.get("altman_z"), retailer).coverage is Coverage.NOT_APPLICABLE
        assert evaluate_one(METRICS.get("altman_z_prime"), retailer).coverage is Coverage.OK

    @pytest.mark.parametrize("sic", [None, "", "not-a-sic"])
    def test_unknown_sic_is_missing_classification_not_structural_na(self, sic):
        snapshot = _snapshot(_altman_frame(), sic=sic)
        assert evaluate_one(METRICS.get("altman_z"), snapshot).coverage is Coverage.MISSING
        assert evaluate_one(METRICS.get("altman_z_prime"), snapshot).coverage is Coverage.MISSING


def test_ohlson_is_not_applicable_to_financial_business_models():
    for sector in (SectorClass.BANK, SectorClass.INSURANCE):
        snapshot = _snapshot(_ohlson_frame(), sector=sector)
        result = evaluate_one(METRICS.get("ohlson_o_score"), snapshot)
        assert result.coverage is Coverage.NOT_APPLICABLE


# -- growth-consistency helpers: registered in every panel, previously untested


def test_r_squared_loglinear_rewards_steady_compounding():
    """Two series with one CAGR: perfect compounding scores 1, a rollercoaster less.

    Both feed `revenue_growth_consistency` / `earnings_growth_consistency` in the
    growth pillar of every frozen panel; panels store only the aggregate score,
    so a sign error here would ship invisibly.
    """
    from stock_grader.metrics.util import r_squared_loglinear

    steady = [100.0 * (1.15**i) for i in range(6)]
    assert r_squared_loglinear(steady) == pytest.approx(1.0)

    rollercoaster = [100.0, 40.0, 180.0, 60.0, 190.0, 201.14]
    bumpy = r_squared_loglinear(rollercoaster)
    assert bumpy is not None and bumpy < 0.7

    # Guard rails: logs need positives, a fit needs three points, a flat series
    # has no variance to explain.
    assert r_squared_loglinear([100.0, -5.0, 120.0]) is None
    assert r_squared_loglinear([100.0, 110.0]) is None
    assert r_squared_loglinear([100.0, 100.0, 100.0]) is None


def test_consistency_is_the_fraction_of_positive_changes():
    from stock_grader.metrics.util import consistency

    assert consistency([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
    assert consistency([4.0, 3.0, 2.0, 1.0]) == pytest.approx(0.0)
    assert consistency([1.0, 2.0, 1.0, 2.0, 1.0]) == pytest.approx(0.5)
    # Non-finite entries are excluded, not treated as zero change.
    assert consistency([1.0, float("nan"), 2.0, 3.0]) == pytest.approx(1.0)
    assert consistency([1.0]) is None
    assert consistency([]) is None
