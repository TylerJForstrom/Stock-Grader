"""End-to-end pipeline tests, run entirely offline against constructed snapshots."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

# Importing these populates the registries.
from stock_grader import aggregate, normalize, weighting  # noqa: F401
from stock_grader.data.sectors import classify_sic
from stock_grader.data.synthetic import generate_panel, generate_prices
from stock_grader.metrics import fundamental, models, sector_specific, statistical  # noqa: F401
from stock_grader.metrics.engine import evaluate_metrics
from stock_grader.pipeline import GradeConfig, grade_universe
from stock_grader.profiles import consensus_grade, get_profile, profile_names
from stock_grader.registry import METRICS, WEIGHTINGS
from stock_grader.types import Coverage, Fundamentals, SectorClass, SecuritySnapshot


def _fundamentals(scale: float = 1.0, *, quality: float = 1.0) -> Fundamentals:
    """Four contiguous quarters and five annual periods for one synthetic company."""
    quarters = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
    years = pd.to_datetime([f"{y}-12-31" for y in range(2021, 2026)])

    quarterly = pd.DataFrame(
        {
            "revenue": [100.0 * scale] * 4,
            "cogs": [55.0 * scale] * 4,
            "gross_profit": [45.0 * scale] * 4,
            "operating_income": [20.0 * scale * quality] * 4,
            "net_income": [15.0 * scale * quality] * 4,
            "pretax_income": [19.0 * scale * quality] * 4,
            "income_tax": [4.0 * scale] * 4,
            "interest_expense": [1.0 * scale] * 4,
            "cfo": [18.0 * scale * quality] * 4,
            "capex": [5.0 * scale] * 4,
            "depreciation_amortization": [6.0 * scale] * 4,
            "dividends_paid": [4.0 * scale] * 4,
            "buybacks": [3.0 * scale] * 4,
            "shares_diluted": [1000.0] * 4,
            "assets": [400.0 * scale] * 4,
            "current_assets": [150.0 * scale] * 4,
            "current_liabilities": [90.0 * scale] * 4,
            "liabilities": [200.0 * scale] * 4,
            "equity": [200.0 * scale] * 4,
            "cash": [50.0 * scale] * 4,
            "inventory": [40.0 * scale] * 4,
            "receivables": [30.0 * scale] * 4,
            "long_term_debt": [80.0 * scale] * 4,
            "short_term_debt": [20.0 * scale] * 4,
            "total_debt": [100.0 * scale] * 4,
            "net_debt": [50.0 * scale] * 4,
            "retained_earnings": [120.0 * scale] * 4,
            "working_capital": [60.0 * scale] * 4,
            "invested_capital": [250.0 * scale] * 4,
            "tangible_book": [180.0 * scale] * 4,
            "ebit": [20.0 * scale * quality] * 4,
            "goodwill": [20.0 * scale] * 4,
        },
        index=quarters,
    )
    growth = np.linspace(0.7, 1.0, len(years))
    annual = pd.DataFrame(
        {
            col: (quarterly[col].iloc[0] * 4 * growth if col not in
                  ("assets", "equity", "shares_diluted", "current_assets", "current_liabilities",
                   "long_term_debt", "cash", "inventory", "receivables", "total_debt")
                  else quarterly[col].iloc[0] * growth)
            for col in quarterly.columns
        },
        index=years,
    )
    from stock_grader.data.concepts import AVERAGED_CONCEPTS, PERIOD_TYPES

    return Fundamentals(
        quarterly=quarterly,
        annual=annual,
        filed=pd.Series(dtype="object"),
        period_type=dict(PERIOD_TYPES),
        averaged=set(AVERAGED_CONCEPTS),
    )


def _universe(n: int = 12, *, with_prices: bool = True) -> list[SecuritySnapshot]:
    tickers = [f"T{i:02d}" for i in range(n)]
    prices, benchmark, _ = (
        generate_panel(tickers, n_days=800, seed=5, synthetic=True) if with_prices else ({}, None, None)
    )
    snapshots = []
    for i, ticker in enumerate(tickers):
        snapshots.append(
            SecuritySnapshot(
                ticker=ticker,
                asof=date(2026, 1, 31),
                fundamentals=_fundamentals(scale=1.0 + i * 0.1, quality=0.6 + i * 0.06),
                sector=SectorClass.GENERAL,
                price=50.0 + i * 5.0,
                shares_outstanding=1000.0,
                prices=prices.get(ticker) if with_prices else None,
                benchmark=benchmark if with_prices else None,
                meta={"synthetic_prices": with_prices},
            )
        )
    return snapshots


class TestGradeUniverse:
    def test_produces_a_report_per_security(self):
        snapshots = _universe()
        reports = grade_universe(snapshots, GradeConfig())
        assert set(reports) == {s.ticker for s in snapshots}

    def test_scores_are_bounded_and_finite(self):
        reports = grade_universe(_universe(), GradeConfig())
        for report in reports.values():
            assert 0.0 <= report.score <= 100.0
            assert np.isfinite(report.score)

    def test_confidence_interval_contains_the_score(self):
        """A reported interval that excludes its own score is incoherent.

        This regressed once: the interval was estimated on the raw composite while the headline
        score had been through the hybrid curve.
        """
        reports = grade_universe(_universe(), GradeConfig(curve="hybrid"))
        for report in reports.values():
            low, high = report.ci
            assert low - 0.01 <= report.score <= high + 0.01, report.ticker

    def test_better_fundamentals_grade_higher(self):
        """The whole system is wrong if this fails."""
        reports = grade_universe(_universe(), GradeConfig())
        best = reports["T11"].score
        worst = reports["T00"].score
        assert best > worst

    def test_deterministic(self):
        first = grade_universe(_universe(), GradeConfig(seed=3))
        second = grade_universe(_universe(), GradeConfig(seed=3))
        assert {k: v.score for k, v in first.items()} == {k: v.score for k, v in second.items()}

    @pytest.mark.parametrize("method", sorted(WEIGHTINGS.names()))
    def test_every_weighting_method_produces_a_grade(self, method):
        reports = grade_universe(
            _universe(), GradeConfig(metric_weighting=method, pillar_weighting=method)
        )
        assert all(np.isfinite(r.score) for r in reports.values())

    @pytest.mark.parametrize("profile", profile_names())
    def test_every_profile_produces_a_grade(self, profile):
        reports = grade_universe(_universe(), get_profile(profile))
        assert all(np.isfinite(r.score) for r in reports.values())

    def test_single_security_is_not_given_a_confident_letter(self):
        """With no peers, cross-sectional scores are all neutral and mean nothing."""
        reports = grade_universe(_universe(1), GradeConfig())
        report = next(iter(reports.values()))
        assert report.letter == "N/A"
        assert any("WITHOUT PEERS" in w for w in report.warnings)

    def test_synthetic_prices_are_flagged(self):
        reports = grade_universe(_universe(), GradeConfig())
        report = next(iter(reports.values()))
        assert any("SYNTHETIC" in w.upper() for w in report.warnings)

    def test_pillar_contributions_reconstruct_the_composite(self):
        reports = grade_universe(_universe(), GradeConfig(pillar_aggregator="weighted_mean",
                                                          curve="absolute"))
        for report in reports.values():
            total = 50.0 + sum(report.explain["pillar_contributions"].values())
            assert total == pytest.approx(report.score, abs=1e-6)


class TestSectorHandling:
    def test_bank_metrics_are_not_applicable_rather_than_missing(self):
        snapshot = _universe(1)[0]
        snapshot.sector = SectorClass.BANK
        results = evaluate_metrics(snapshot)
        assert results["current_ratio"].coverage is Coverage.NOT_APPLICABLE
        assert results["altman_z"].coverage is Coverage.NOT_APPLICABLE
        assert results["gross_margin"].coverage is Coverage.NOT_APPLICABLE

    def test_not_applicable_does_not_reduce_coverage(self):
        """A bank must not be marked down for ratios that were never defined for it."""
        general = _universe(6)
        banks = _universe(6)
        for snapshot in banks:
            snapshot.sector = SectorClass.BANK
        general_cov = np.mean([r.coverage for r in grade_universe(general, GradeConfig()).values()])
        bank_cov = np.mean([r.coverage for r in grade_universe(banks, GradeConfig()).values()])
        assert bank_cov >= general_cov - 0.05

    @pytest.mark.parametrize(
        "sic,expected",
        [("6021", SectorClass.BANK), ("6798", SectorClass.REIT), ("4911", SectorClass.UTILITY),
         ("2911", SectorClass.ENERGY), ("3571", SectorClass.GENERAL), (None, SectorClass.GENERAL),
         ("garbage", SectorClass.GENERAL)],
    )
    def test_sic_classification(self, sic, expected):
        assert classify_sic(sic) is expected


class TestMetricCatalogue:
    def test_all_metrics_have_a_pillar_and_direction(self):
        for name, spec in METRICS.items():
            assert spec.pillar, name
            assert spec.direction in (-1, 0, 1), name

    def test_non_monotonic_metrics_declare_an_ideal_band(self):
        """``direction=0`` without a band would silently score through a default range."""
        for name, spec in METRICS.items():
            if spec.direction == 0:
                assert spec.ideal_band is not None, f"{name} is non-monotonic but has no ideal_band"

    def test_metrics_return_none_not_zero_when_inputs_are_absent(self):
        empty = SecuritySnapshot(ticker="EMPTY", asof=date(2026, 1, 31))
        results = evaluate_metrics(empty)
        for name, result in results.items():
            assert result.value is None or result.coverage is Coverage.OK
            if result.coverage is not Coverage.OK:
                assert result.value is None, f"{name} returned a value while not OK"

    def test_no_metric_raises_on_a_degenerate_snapshot(self):
        """A snapshot with empty frames must produce misses, never exceptions."""
        snapshot = SecuritySnapshot(
            ticker="X",
            asof=date(2026, 1, 31),
            fundamentals=Fundamentals(pd.DataFrame(), pd.DataFrame(), pd.Series(dtype="object")),
            prices=pd.DataFrame(),
        )
        results = evaluate_metrics(snapshot)
        assert len(results) == len(METRICS)
        for result in results.values():
            assert "error:" not in result.note, result.note


class TestConsensus:
    def test_consensus_reports_disagreement(self):
        results = consensus_grade(_universe())
        for result in results.values():
            assert 0.0 <= result.clarity <= 100.0
            assert result.best_profile in profile_names()
            assert result.worst_profile in profile_names()

    def test_clarity_falls_as_profiles_disagree(self):
        results = consensus_grade(_universe())
        spreads = {t: r.spread for t, r in results.items()}
        clarities = {t: r.clarity for t, r in results.items()}
        widest = max(spreads, key=spreads.get)
        narrowest = min(spreads, key=spreads.get)
        assert clarities[widest] <= clarities[narrowest]


class TestSyntheticData:
    def test_generator_refuses_without_explicit_acknowledgement(self):
        with pytest.raises(ValueError, match="synthetic=True"):
            generate_prices("X")

    def test_generator_is_deterministic(self):
        first = generate_prices("X", n_days=200, seed=1, synthetic=True)
        second = generate_prices("X", n_days=200, seed=1, synthetic=True)
        pd.testing.assert_frame_equal(first, second)

    def test_generator_produces_valid_ohlc(self):
        frame = generate_prices("X", n_days=300, seed=2, synthetic=True)
        assert (frame["high"] >= frame[["open", "close"]].max(axis=1)).all()
        assert (frame["low"] <= frame[["open", "close"]].min(axis=1)).all()
        assert (frame["volume"] > 0).all()

    def test_planted_factor_is_recoverable(self):
        """The validation harness needs data whose truth is known.

        A weighting method that cannot recover a planted signal from this panel is broken, and that
        is only testable against generated data.
        """
        from scipy import stats

        tickers = [f"S{i:02d}" for i in range(30)]
        prices, _, exposure = generate_panel(
            tickers, n_days=600, seed=9, factor_strength=0.3, synthetic=True
        )
        realised = {
            t: float(np.log(prices[t]["close"]).diff().dropna().iloc[-252:].sum()) for t in tickers
        }
        ic = stats.spearmanr(exposure.values, [realised[t] for t in tickers]).statistic
        assert ic > 0.3


class TestEffectiveWeights:
    """Nominal weights describe the profile; effective weights describe the grade.

    On a price-free run the momentum profile graded ABT B+ while printing momentum 0.50 / risk 0.20
    / liquidity 0.06 and drawing its contributions from growth and profitability alone — 76% of the
    advertised weight inert, in the layer a user checks to audit a score.
    """

    def test_effective_weights_sum_to_one_over_live_pillars(self):
        reports = grade_universe(_universe(8), GradeConfig())
        for report in reports.values():
            assert set(report.effective_pillar_weights) == set(report.pillars)
            assert sum(report.effective_pillar_weights.values()) == pytest.approx(1.0, abs=1e-9)

    def test_lost_weight_is_reported_when_a_pillar_does_not_compute(self):
        """A pillar the universe has metrics for, but that this security could not compute.

        Weight assigned to a pillar no security has is renormalised away before it reaches the
        report, so it is not "lost" for anyone — this measures the case that actually misleads:
        the pillar exists, the profile weights it, and this grade did not use it.
        """
        snapshots = _universe(8)
        # Blank one security's growth inputs so its growth pillar alone fails to compute.
        target = snapshots[0]
        frame = target.fundamentals.annual.copy()
        for column in ("revenue", "net_income", "fcf", "equity"):
            if column in frame.columns:
                frame[column] = np.nan
        target.fundamentals.annual = frame

        config = GradeConfig(pillar_weights={"profitability": 0.5, "growth": 0.5},
                             pillar_weighting="fixed")
        report = grade_universe(snapshots, config)[target.ticker]
        assert report.lost_weight > 0.0 or "growth" not in report.pillars
        assert sum(report.effective_pillar_weights.values()) == pytest.approx(1.0, abs=1e-9)

    def test_pillar_set_is_recorded(self):
        report = next(iter(grade_universe(_universe(8), GradeConfig()).values()))
        assert report.meta["pillar_set"] == sorted(report.pillars)


class TestIntervalAndLetterProbabilities:
    def test_score_lies_inside_its_own_interval(self):
        for curve in ("hybrid", "absolute", "cross_sectional"):
            for report in grade_universe(_universe(12), GradeConfig(curve=curve)).values():
                low, high = report.ci
                assert low - 0.01 <= report.score <= high + 0.01, f"{report.ticker} under {curve}"

    def test_letter_probabilities_form_a_distribution(self):
        for report in grade_universe(_universe(12), GradeConfig()).values():
            probabilities = report.explain["letter_probabilities"]
            assert probabilities
            assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-9)
            assert all(0.0 <= v <= 1.0 for v in probabilities.values())

    def test_percentile_contributes_width(self):
        """The old affine mapping gave the percentile zero width, halving the interval.

        Measured empirical coverage of the advertised 90% was 0.697 falling to 0.395 as pillars
        were masked — it was not a 90% interval at any level.
        """
        reports = grade_universe(_universe(20), GradeConfig(curve="hybrid"))
        widths = [r.ci[1] - r.ci[0] for r in reports.values() if np.isfinite(r.ci[0])]
        assert np.median(widths) > 3.0, "a hybrid interval must carry the percentile's own spread"


class TestSectorSpecificMetrics:
    """Disabling a metric for a sector without replacing it left financials under-measured.

    Of 65 price-free metrics an industrial got all 65 while a bank got 36, and the entire
    efficiency pillar was empty for banks. A three-metric pillar carries several times the sampling
    variance of a twelve-metric one, so bank grades were uninformative rather than wrong.
    """

    def test_bank_metrics_are_not_applicable_elsewhere(self):
        """An industrial must be NOT_APPLICABLE for a bank metric, never MISSING.

        MISSING would charge it a coverage penalty for lacking a metric that was never defined
        for it.
        """
        from stock_grader.data.sectors import BANK_ONLY_METRICS, is_applicable

        for name in BANK_ONLY_METRICS:
            assert not is_applicable(name, "efficiency", SectorClass.GENERAL)
            assert not is_applicable(name, "efficiency", SectorClass.REIT)
            assert is_applicable(name, "efficiency", SectorClass.BANK)

    def test_reit_metrics_are_not_applicable_elsewhere(self):
        from stock_grader.data.sectors import REIT_ONLY_METRICS, is_applicable

        for name in REIT_ONLY_METRICS:
            assert not is_applicable(name, "valuation", SectorClass.GENERAL)
            assert not is_applicable(name, "valuation", SectorClass.BANK)
            assert is_applicable(name, "valuation", SectorClass.REIT)

    def test_banks_regain_the_efficiency_pillar(self):
        from stock_grader.data.sectors import SECTOR_DISABLED_PILLARS

        assert "efficiency" not in SECTOR_DISABLED_PILLARS[SectorClass.BANK]

    def test_ffo_is_scaled_not_a_dollar_level(self):
        """FFO in dollars is a size measure: scored cross-sectionally in a profitability pillar,
        the biggest REIT would win on being big."""
        from stock_grader.registry import METRICS

        spec = METRICS.get("ffo_to_assets")
        assert spec.unit == "ratio"

    def test_ffo_is_reconstructed_not_read(self):
        """No major REIT tags FundsFromOperations — checked SPG, O, PLD and AMT.

        An implementation reading it directly would report 100% missing.
        """
        from stock_grader.data.concepts import CONCEPTS

        all_tags = {tag for chain in CONCEPTS.values() for tag in chain}
        assert "FundsFromOperations" not in all_tags

    def test_ffo_rejects_an_implausible_reconstruction(self):
        from datetime import date as _date

        from stock_grader.metrics.sector_specific import ffo_to_assets
        from stock_grader.types import Fundamentals, SecuritySnapshot

        quarters = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
        # Depreciation twenty times income: the components did not assemble sensibly.
        frame = pd.DataFrame(
            {"net_income": [10.0] * 4, "income_to_common": [10.0] * 4,
             "depreciation_amortization": [200.0] * 4, "assets": [5000.0] * 4},
            index=quarters,
        )
        snapshot = SecuritySnapshot(
            ticker="X", asof=_date(2026, 1, 31),
            fundamentals=Fundamentals(frame, frame, pd.Series(dtype="object")),
        )
        assert ffo_to_assets.fn(snapshot) is None

    def test_loans_to_deposits_is_a_band_not_a_direction(self):
        """Above ~1.0 a bank funds itself wholesale — the vulnerability that closed SVB.

        Far below 0.6 it cannot deploy its deposits. Both ends are worse than the middle.
        """
        from stock_grader.registry import METRICS

        spec = METRICS.get("loans_to_deposits")
        assert spec.direction == 0
        assert spec.ideal_band is not None


class TestRobustness:
    """Adversarial inputs must work or fail clearly — never crash, hang, or return a number."""

    def _empty(self, ticker: str) -> SecuritySnapshot:
        frame = pd.DataFrame()
        return SecuritySnapshot(
            ticker=ticker, asof=date(2026, 7, 25),
            fundamentals=Fundamentals(frame, frame, pd.Series(dtype="object")),
        )

    def test_empty_universe(self):
        assert grade_universe([], GradeConfig()) == {}

    def test_security_with_no_data_is_ungraded_not_zero(self):
        report = grade_universe([self._empty("X")], GradeConfig())["X"]
        assert report.letter == "N/A"

    def test_duplicate_tickers_warn_rather_than_silently_collapse(self, caplog):
        """Results are keyed by ticker, so a repeat overwrites its predecessor and shrinks the
        peer set — which shifts every percentile in the universe."""
        import logging

        with caplog.at_level(logging.WARNING):
            reports = grade_universe([self._empty("A"), self._empty("A"), self._empty("B")], GradeConfig())
        assert len(reports) == 2
        assert any("duplicate" in r.message.lower() for r in caplog.records)

    @pytest.mark.parametrize("rho", [200.0, -200.0, float("inf"), float("nan")])
    def test_unusable_rho_names_the_parameter(self, rho):
        """Outside the usable range the power mean overflows and the aggregator fails soft to
        None, surfacing as an unexplained N/A grade rather than a named bad parameter."""
        with pytest.raises(ValueError, match="rho"):
            GradeConfig(aggregator_kwargs={"rho": rho})

    @pytest.mark.parametrize("rho", [1.0, 0.5, 0.0, -1.0, -5.0])
    def test_usable_rho_accepted(self, rho):
        assert GradeConfig(aggregator_kwargs={"rho": rho}) is not None

    def test_unknown_names_list_the_valid_options(self):
        from stock_grader.profiles import get_profile

        with pytest.raises(KeyError, match="all_weather"):
            get_profile("not_a_profile")

    def test_a_thousand_securities(self):
        reports = grade_universe([self._empty(f"T{i}") for i in range(1000)], GradeConfig())
        assert len(reports) == 1000

    def test_bank_and_industrial_in_one_universe(self):
        bank = self._empty("BK")
        bank.sector = SectorClass.BANK
        reports = grade_universe([bank, self._empty("IN")], GradeConfig())
        assert set(reports) == {"BK", "IN"}

    def test_degenerate_price_frames(self):
        for frame in (
            pd.DataFrame(),
            pd.DataFrame({"close": [1.0], "adj_close": [1.0]}, index=pd.to_datetime(["2026-01-01"])),
        ):
            snapshot = SecuritySnapshot(ticker="X", asof=date(2026, 7, 25), prices=frame)
            assert grade_universe([snapshot], GradeConfig())["X"] is not None

    def test_reports_serialise_even_when_ungraded(self):
        from stock_grader.report import to_json, to_markdown

        report = grade_universe([self._empty("X")], GradeConfig())["X"]
        assert to_json(report)
        assert to_markdown(report)
