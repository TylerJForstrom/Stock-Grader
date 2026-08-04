"""End-to-end pipeline tests, run entirely offline against constructed snapshots."""

from __future__ import annotations

import copy
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

import stock_grader.pipeline as pipeline_module

# Importing these populates the registries.
from stock_grader import aggregate, normalize, weighting  # noqa: F401
from stock_grader.data.sectors import classify_sic
from stock_grader.data.synthetic import generate_panel, generate_prices
from stock_grader.metrics import fundamental, models, sector_specific, statistical  # noqa: F401
from stock_grader.metrics.engine import evaluate_metrics
from stock_grader.pipeline import GradeConfig, grade_universe, grade_universe_multi
from stock_grader.profiles import consensus_grade, get_profile, profile_names
from stock_grader.registry import METRICS, WEIGHTINGS
from stock_grader.types import Coverage, Fundamentals, PitMode, SectorClass, SecuritySnapshot


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


def _universe(n: int = 16, *, with_prices: bool = True) -> list[SecuritySnapshot]:
    # 16 >= GradeConfig.min_letter_peers: the default fixture must exercise the
    # actually-graded path; the letter floor has its own dedicated tests.
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


def _assert_matching_report_fields(actual, expected) -> None:
    assert actual.letter == expected.letter
    assert actual.graded is expected.graded
    assert actual.score == pytest.approx(expected.score, nan_ok=True)
    assert actual.coverage == pytest.approx(expected.coverage, nan_ok=True)
    if expected.percentile is None:
        assert actual.percentile is None
    else:
        assert actual.percentile == pytest.approx(expected.percentile, nan_ok=True)
    assert actual.meta.get("config_fingerprint") == expected.meta.get("config_fingerprint")
    assert actual.meta.get("universe_fingerprint") == expected.meta.get("universe_fingerprint")


def test_grade_universe_multi_matches_per_profile_grade_universe() -> None:
    snapshots = _universe(4, with_prices=False)
    configs = [get_profile(name) for name in profile_names()]

    combined = grade_universe_multi(snapshots, configs)

    assert set(combined) == set(profile_names())
    for config in configs:
        individual = grade_universe(snapshots, config)
        assert set(combined[config.name]) == set(individual)
        for ticker, expected in individual.items():
            _assert_matching_report_fields(combined[config.name][ticker], expected)


def test_grade_universe_multi_builds_and_normalizes_once_for_shared_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"build": 0, "normalize": 0}
    original_build = pipeline_module.build_metric_matrix
    original_normalize = pipeline_module._normalize_matrix

    def counted_build(*args, **kwargs):
        calls["build"] += 1
        return original_build(*args, **kwargs)

    def counted_normalize(*args, **kwargs):
        calls["normalize"] += 1
        return original_normalize(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "build_metric_matrix", counted_build)
    monkeypatch.setattr(pipeline_module, "_normalize_matrix", counted_normalize)

    grade_universe_multi(
        _universe(3, with_prices=False),
        [get_profile(name) for name in profile_names()],
    )

    assert calls == {"build": 1, "normalize": 1}


def test_grade_universe_multi_unions_then_restores_metric_whitelists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = _universe(3, with_prices=False)
    configs = [
        GradeConfig(
            name="health_only",
            metric_whitelist=["current_ratio"],
            pillar_weights={"health": 1.0},
            gates=False,
            min_letter_peers=2,
        ),
        GradeConfig(
            name="shareholder_only",
            metric_whitelist=["payout_ratio"],
            pillar_weights={"shareholder": 1.0},
            gates=False,
            min_letter_peers=2,
        ),
        GradeConfig(name="empty", metric_whitelist=[]),
    ]
    expected = {config.name: grade_universe(snapshots, config) for config in configs}
    requested_names: list[list[str] | None] = []
    original_build = pipeline_module.build_metric_matrix

    def capture_names(*args, **kwargs):
        requested_names.append(kwargs.get("names"))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "build_metric_matrix", capture_names)
    combined = grade_universe_multi(snapshots, configs)

    assert requested_names == [["current_ratio", "payout_ratio"]]
    for config in configs:
        for ticker, expected_report in expected[config.name].items():
            _assert_matching_report_fields(combined[config.name][ticker], expected_report)



def test_clean_dated_frame_cache_matches_uncached() -> None:
    periods = pd.to_datetime(["2024-12-31", "2025-03-31"])
    quarterly = pd.DataFrame({"revenue": [10.0, 20.0]}, index=periods)
    filed = pd.Series(pd.to_datetime(["2025-02-01", "2025-05-01"]), index=periods)
    fundamentals = Fundamentals(
        quarterly=quarterly,
        annual=quarterly.copy(),
        filed=filed,
        pit_mode=PitMode.PIT,
    )
    asof = date(2025, 4, 1)

    first = fundamentals._clean_dated_frame(quarterly, ["revenue"], asof=asof)
    cached = fundamentals._clean_dated_frame(quarterly, ["revenue"], asof=asof)
    assert first is cached
    assert first is not None
    assert list(first.index) == [pd.Timestamp("2024-12-31")]

    fundamentals._frame_cache.clear()
    uncached = fundamentals._clean_dated_frame(quarterly, ["revenue"], asof=asof)
    pd.testing.assert_frame_equal(cached, uncached)

    assert fundamentals._clean_dated_frame(quarterly, ["missing"], asof=asof) is None
    assert any(entry[2] is None for entry in fundamentals._frame_cache.values())


def test_clean_dated_frame_cache_does_not_leak_across_shallow_copy_or_pit_mode() -> None:
    periods = pd.to_datetime(["2024-12-31", "2025-03-31"])
    quarterly = pd.DataFrame({"revenue": [10.0, 20.0]}, index=periods)
    filed = pd.Series(pd.to_datetime(["2025-02-01", "2025-05-01"]), index=periods)
    fundamentals = Fundamentals(quarterly, quarterly.copy(), filed, pit_mode=PitMode.PIT)
    asof = date(2025, 4, 1)
    fundamentals._clean_dated_frame(quarterly, ["revenue"], asof=asof)

    replaced = copy.copy(fundamentals)
    replaced_frame = quarterly.copy()
    replaced_frame.loc[periods[0], "revenue"] = 999.0
    replaced.quarterly = replaced_frame
    replaced_result = replaced._clean_dated_frame(replaced_frame, ["revenue"], asof=asof)
    assert replaced_result is not None
    assert replaced_result.iloc[0, 0] == pytest.approx(999.0)

    latest = copy.copy(fundamentals)
    latest.pit_mode = PitMode.LATEST
    latest_result = latest._clean_dated_frame(quarterly, ["revenue"], asof=asof)
    assert latest_result is not None
    assert len(latest_result) == 2


def test_sector_neutral_key_validates_and_default_preserves_business_model() -> None:
    with pytest.raises(ValueError, match="sector_neutral_key"):
        GradeConfig(sector_neutral_key=["sic2"])
    with pytest.raises(ValueError, match="sector_neutral_key"):
        GradeConfig(sector_neutral_key="gics")

    snapshots = [
        SecuritySnapshot(
            ticker=f"T{index}",
            asof=date(2026, 1, 31),
            sic=(f"35{index:02d}" if index < 5 else f"60{index:02d}"),
            sector=SectorClass.GENERAL,
        )
        for index in range(10)
    ]
    matrix = pd.DataFrame(
        {"current_ratio": [1.0, 2.0, 3.0, 4.0, 5.0, 101.0, 102.0, 103.0, 104.0, 105.0]},
        index=[snapshot.ticker for snapshot in snapshots],
    )

    default_scores = pipeline_module._normalize_matrix(matrix, snapshots, GradeConfig())
    explicit_scores = pipeline_module._normalize_matrix(
        matrix,
        snapshots,
        GradeConfig(sector_neutral_key="business_model"),
    )
    sic_scores = pipeline_module._normalize_matrix(
        matrix,
        snapshots,
        GradeConfig(sector_neutral_key="sic2"),
    )

    pd.testing.assert_frame_equal(default_scores, explicit_scores)
    assert not np.allclose(default_scores["current_ratio"], sic_scores["current_ratio"])


def test_default_sector_key_preserves_frozen_panel_config_fingerprint() -> None:
    # DELIBERATE retirement of 751441e6…: the defining-pillar coverage floor
    # (min_defining_pillar_coverage, default 0.4) changes which names the gate
    # refuses, so panels frozen before and after it are not comparable — and the
    # fingerprint is the contract that records exactly that. Panels through
    # 2026-08 carry the old hash; do NOT "fix" this value to make them match.
    expected = "1790775dfae6ddeb8feaf9649142d8b8c1eb20d8a3a26832b08695d0b4403846"

    implicit_manifest, implicit_fingerprint = pipeline_module._config_manifest(
        get_profile("all_weather")
    )
    explicit_manifest, explicit_fingerprint = pipeline_module._config_manifest(
        get_profile("all_weather", sector_neutral_key="business_model")
    )
    sic2_manifest, sic2_fingerprint = pipeline_module._config_manifest(
        get_profile("all_weather", sector_neutral_key="sic2")
    )
    sic3_manifest, sic3_fingerprint = pipeline_module._config_manifest(
        get_profile("all_weather", sector_neutral_key="sic3")
    )

    assert implicit_fingerprint == explicit_fingerprint == expected
    assert "sector_neutral_key" not in implicit_manifest
    assert "sector_neutral_key" not in explicit_manifest
    assert sic2_manifest["sector_neutral_key"] == "sic2"
    assert sic3_manifest["sector_neutral_key"] == "sic3"
    assert len({expected, sic2_fingerprint, sic3_fingerprint}) == 3


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

    def test_sensitivity_range_contains_the_score(self):
        """A reported sensitivity range that excludes its own baseline score is incoherent.

        This regressed once: the range was estimated on the raw composite while the headline score
        had been through the hybrid curve.
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
        snapshots = _universe()
        # The catalogue smoke test supplies realised returns so supervised methods genuinely
        # fit (in-sample, explicitly authorized) instead of silently degrading to equal
        # weights — selecting one without returns is an input-error refusal, tested below.
        # Production configs remain blocked by default and are tested separately below.
        realised = pd.Series(
            {snapshot.ticker: float(i) for i, snapshot in enumerate(snapshots)},
            dtype="float64",
        )
        reports = grade_universe(
            snapshots,
            GradeConfig(
                metric_weighting=method,
                pillar_weighting=method,
                allow_in_sample_supervised_weighting=True,
            ),
            forward_returns=realised,
        )
        assert all(np.isfinite(r.score) for r in reports.values())

    @pytest.mark.parametrize(
        "method",
        sorted(
            name
            for name, info in weighting.WEIGHT_METHOD_INFO.items()
            if info.get("needs_returns")
        ),
    )
    def test_supervised_weighting_requires_explicit_research_authorization(self, method):
        snapshots = _universe()
        realised = pd.Series(
            {snapshot.ticker: float(i) for i, snapshot in enumerate(snapshots)},
            dtype="float64",
        )
        with pytest.raises(ValueError, match="in-sample supervised weighting is blocked"):
            grade_universe(
                snapshots,
                GradeConfig(metric_weighting=method, pillar_weighting=method),
                forward_returns=realised,
            )

    @pytest.mark.parametrize(
        "method",
        sorted(
            name
            for name, info in weighting.WEIGHT_METHOD_INFO.items()
            if info.get("needs_returns")
        ),
    )
    def test_supervised_weighting_without_returns_is_refused_not_degraded(self, method):
        """No forward returns is an input error, not a silent equal-weights fallback.

        The research opt-in flag must not silence it either: that flag authorizes fitting
        in-sample returns, not running a supervised method with nothing to fit — which would
        report the supervised method's name over plain equal weights.
        """
        with pytest.raises(ValueError, match="no forward returns supplied"):
            grade_universe(
                _universe(),
                GradeConfig(metric_weighting=method, pillar_weighting=method),
            )
        with pytest.raises(ValueError, match="no forward returns supplied"):
            grade_universe(
                _universe(),
                GradeConfig(
                    metric_weighting=method,
                    pillar_weighting=method,
                    allow_in_sample_supervised_weighting=True,
                ),
            )

    @pytest.mark.parametrize("profile", profile_names())
    def test_every_profile_produces_a_grade(self, profile):
        reports = grade_universe(_universe(), get_profile(profile))
        assert all(np.isfinite(r.score) for r in reports.values())

    def test_single_security_is_not_given_an_unsupported_letter(self):
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

    def test_ces_contributions_reconstruct_then_curve_effect_reaches_headline(self):
        reports = grade_universe(_universe(), GradeConfig(curve="hybrid"))
        for report in reports.values():
            standardized = 50.0 + sum(report.explain["pillar_contributions"].values())
            assert standardized == pytest.approx(
                report.explain["standardized_composite"], abs=1e-8
            )
            assert standardized + report.explain["peer_rank_curve_effect"] == pytest.approx(
                report.score, abs=1e-8
            )

    def test_tied_points_and_unchanged_draws_use_the_same_midrank(self):
        config = GradeConfig(
            name="tie_regression",
            metric_whitelist=["payout_ratio"],
            pillar_weights={"shareholder": 1.0},
            pillar_aggregator="weighted_mean",
            curve="hybrid",
            uncertainty_draws=1,
        )
        reports = grade_universe(_universe(2, with_prices=False), config)
        for report in reports.values():
            assert report.percentile == pytest.approx(50.0)
            assert report.score == pytest.approx(75.0)
            assert report.ci == pytest.approx((75.0, 75.0))
            assert report.explain["letter_scenario_frequencies"] == {"B+": 1.0}


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
            assert result.best_profile in profile_names()
            assert result.worst_profile in profile_names()
            assert math.isfinite(result.spread)

    def test_letter_distribution_counts_graded_profiles(self):
        # The distribution replaced the invented clarity scalar: it must count
        # every graded profile exactly once and use real letters.
        results = consensus_grade(_universe())
        for result in results.values():
            assert sum(result.letter_distribution.values()) == len(result.scores)
            valid = {"A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"}
            assert set(result.letter_distribution) <= valid
            assert "clarity" not in result.to_dict()


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


class TestProfileCoverageGates:
    def test_profile_coverage_uses_authored_weights_not_run_renormalization(self):
        config = GradeConfig(
            name="coverage_regression",
            metric_whitelist=["payout_ratio"],
            pillar_weights={"shareholder": 0.69, "momentum": 0.31},
            min_profile_weight_coverage=0.70,
        )
        reports = grade_universe(_universe(2, with_prices=False), config)
        for report in reports.values():
            assert report.letter == "N/A"
            assert report.explain["profile_weight_coverage"] == pytest.approx(0.69)
            assert any(reason.startswith("profile_weight_coverage:") for reason in report.gates)

    def test_explicit_defining_pillar_is_a_hard_gate_with_a_reason(self):
        config = GradeConfig(
            name="required_regression",
            metric_whitelist=["payout_ratio"],
            pillar_weights={"shareholder": 1.0},
            required_pillars={"momentum"},
            min_profile_weight_coverage=0.0,
        )
        reports = grade_universe(_universe(2, with_prices=False), config)
        for report in reports.values():
            assert report.letter == "N/A"
            assert "defining_pillar_missing:momentum" in report.gates

    def test_momentum_profile_refuses_to_substitute_fundamentals_for_missing_momentum(self):
        reports = grade_universe(_universe(8, with_prices=False), get_profile("momentum"))
        for report in reports.values():
            assert report.letter == "N/A"
            assert any(reason.startswith("profile_weight_coverage:") for reason in report.gates)
            assert "defining_pillar_missing:momentum" in report.gates


class TestSensitivityAndLetterScenarioFrequencies:
    def test_score_lies_inside_its_own_sensitivity_range(self):
        for curve in ("hybrid", "absolute", "cross_sectional"):
            for report in grade_universe(_universe(12), GradeConfig(curve=curve)).values():
                low, high = report.ci
                assert low - 0.01 <= report.score <= high + 0.01, f"{report.ticker} under {curve}"

    def test_letter_scenario_frequencies_form_a_distribution(self):
        for report in grade_universe(_universe(12), GradeConfig()).values():
            frequencies = report.explain["letter_scenario_frequencies"]
            assert frequencies
            assert sum(frequencies.values()) == pytest.approx(1.0, abs=1e-9)
            assert all(0.0 <= value <= 1.0 for value in frequencies.values())

    def test_percentile_contributes_width(self):
        """The old affine mapping gave the percentile zero width, halving the range.

        It discarded rank movement, so it understated sensitivity increasingly badly as pillars
        were masked.
        """
        reports = grade_universe(_universe(20), GradeConfig(curve="hybrid"))
        widths = [r.ci[1] - r.ci[0] for r in reports.values() if np.isfinite(r.ci[0])]
        assert np.median(widths) > 3.0, "a hybrid range must carry the percentile's own spread"


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


class TestPercentileBase:
    """A security we refuse to grade cannot be a peer for a comparison we decline to make."""

    def test_ungradeable_securities_do_not_occupy_rank(self):
        from stock_grader.types import Fundamentals

        graded = _universe(6)
        empty = pd.DataFrame()
        blanks = [
            SecuritySnapshot(
                ticker=f"BLANK{i}", asof=date(2026, 1, 31),
                fundamentals=Fundamentals(empty, empty, pd.Series(dtype="object")),
            )
            for i in range(6)
        ]
        with_blanks = grade_universe(graded + blanks, GradeConfig())
        without = grade_universe(graded, GradeConfig())
        for ticker in [s.ticker for s in graded]:
            a = with_blanks[ticker].percentile
            b = without[ticker].percentile
            if a is not None and b is not None:
                assert abs(a - b) < 1e-6, f"{ticker} percentile shifted by ungradeable peers"


class TestBeneishPlausibility:
    """Beneish's indices are year-over-year ratios; a real company sits near 1.0.

    Lowe's produced a DSRI of 11.63 when its receivables tag changed, carrying M to +7.65 and
    flagging one of the largest retailers in the US as an earnings manipulator.
    """

    def test_implausible_index_is_refused(self):
        from stock_grader.metrics.models import _INDEX_PLAUSIBLE, _index

        low, high = _INDEX_PLAUSIBLE
        assert _index(1.0, 1.0) == pytest.approx(1.0)
        assert _index(1.4, 1.0) == pytest.approx(1.4)     # Beneish's own manipulator mean
        assert _index(11.63, 1.0) is None                 # the Lowe's artifact
        assert _index(1.0, 100.0) is None
        assert low < 1.0 < high


def test_defining_pillar_must_clear_a_coverage_floor():
    """Regression: a 1-of-12-metric pillar must not satisfy the defining gate.

    Pre-fix, the gate only asked whether the required pillar was LIVE — one
    computed metric out of twelve kept a "value" profile gradeable on a single
    number wearing the pillar's name.
    """
    config = GradeConfig(name="value_like", pillar_weights={"valuation": 0.7, "quality": 0.3})
    weights = pd.Series({"valuation": 0.7, "quality": 0.3})

    # One metric of twelve computed: live, but not the stated style.
    _, _, thin = pipeline_module._profile_gate_state(
        {"valuation", "quality"}, weights, config, {"valuation": 1 / 12, "quality": 0.9}
    )
    assert any(reason.startswith("defining_pillar_coverage:valuation") for reason in thin)

    # Ample coverage: no coverage reason.
    _, _, healthy = pipeline_module._profile_gate_state(
        {"valuation", "quality"}, weights, config, {"valuation": 0.75, "quality": 0.9}
    )
    assert not any(r.startswith("defining_pillar_coverage") for r in healthy)

    # A MISSING required pillar still reports the missing reason, not coverage.
    _, _, missing = pipeline_module._profile_gate_state(
        {"quality"}, weights, config, {"quality": 0.9}
    )
    assert any(r.startswith("defining_pillar_missing:valuation") for r in missing)

    # The floor is part of the comparability contract.
    manifest, _ = pipeline_module._config_manifest(config)
    assert manifest["min_defining_pillar_coverage"] == pytest.approx(0.4)
