from __future__ import annotations

from argparse import Namespace
from datetime import date

import pytest

from stock_grader import cli
from stock_grader.pipeline import GradeConfig
from stock_grader.types import PitMode, SecuritySnapshot


def _snapshot_args() -> Namespace:
    return Namespace(
        price_provider="none",
        price_dir=None,
        stockanalysis=False,
        no_network=True,
        cache_dir=None,
        contact=None,
        refresh=False,
        benchmark="SP500",
        sec_prices=False,
        price=[],
        asof=None,
        pit=False,
        synthetic_prices=False,
        max_price_age=400,
    )


def test_historical_cik_and_ticker_paths_share_latest_vintage_guard():
    from stock_grader.data.sec import SECProvider

    provider = SECProvider.__new__(SECProvider)
    # The guard runs before either network/client access in the shared _fetch body.
    with pytest.raises(ValueError, match="PitMode.PIT"):
        provider._fetch(  # noqa: SLF001 - direct invariant test
            "0000886158",
            "886158",
            asof=date(2019, 1, 1),
            pit_mode=PitMode.LATEST,
        )


def test_numeric_cik_is_routed_to_fetch_by_cik():
    calls: list[tuple[str, str | None]] = []

    class Provider:
        def fetch_by_cik(self, cik, *, ticker=None, **_kwargs):
            calls.append((cik, ticker))
            return SecuritySnapshot(ticker=ticker or cik, asof=date.today(), cik=cik)

        def fetch(self, *_args, **_kwargs):
            raise AssertionError("numeric CIK must not use ticker resolution")

    snapshots = cli._build_snapshots(["0000886158"], _snapshot_args(), provider=Provider())

    assert len(snapshots) == 1
    assert calls == [("0000886158", "0000886158")]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"curve": "invented"}, "unknown curve"),
        ({"normalizer": "piecewise"}, "metric-specific calibrated anchors"),
        ({"normalizer": "double_sigmoid"}, "metric-specific ideal band"),
        ({"absolute_weight": 1.5}, "between 0 and 1"),
        ({"uncertainty_draws": 0}, "positive integer"),
        ({"metric_weighting": "missing"}, "unknown metric_weighting"),
        ({"metric_aggregator": "missing"}, "unknown metric_aggregator"),
        ({"metric_whitelist": ["not_a_metric"]}, "unknown metrics"),
        ({"metric_whitelist": ["pe_ratio", "pe_ratio"]}, "duplicate metric names"),
        ({"pillar_weights": {"typo": 1.0}}, "unknown pillars"),
        ({"pillar_weights": {"valuation": -1.0}}, "negative or non-finite"),
        ({"pillar_weights": {"valuation": float("nan")}}, "negative or non-finite"),
        (
            {"metric_weights": {"valuation": {"gross_margin": 1.0}}},
            "metrics from another pillar",
        ),
        (
            {"metric_weights": {"valuation": {"not_a_metric": 1.0}}},
            "unknown metrics",
        ),
        ({"required_pillars": {"typo"}}, "unknown pillars"),
        ({"aggregator_kwargs": {"rhp": 0.5}}, "not used"),
        (
            {
                "pillar_aggregator": "owa",
                "aggregator_kwargs": {"pessimism": 1.5},
            },
            "pessimism",
        ),
        ({"min_profile_weight_coverage": float("nan")}, "between 0 and 1"),
    ],
)
def test_grade_config_fails_fast_on_non_operational_settings(kwargs, message):
    with pytest.raises(ValueError, match=message):
        GradeConfig(**kwargs)


def test_cli_hides_methods_that_require_unavailable_configuration():
    parser = cli.build_parser()
    grade = next(
        action
        for action in parser._subparsers._group_actions  # noqa: SLF001
        if action.dest == "command"
    ).choices["grade"]
    weighting = next(action for action in grade._actions if action.dest == "weighting")  # noqa: SLF001
    normalizer = next(action for action in grade._actions if action.dest == "normalizer")  # noqa: SLF001

    assert "ic" not in weighting.choices
    assert "fixed" not in weighting.choices
    assert "ahp" not in weighting.choices
    assert "piecewise" not in normalizer.choices
    assert "double_sigmoid" not in normalizer.choices


def test_research_parser_exposes_peer_and_valuation_assumptions():
    args = cli.build_parser().parse_args(
        [
            "research",
            "AAPL",
            "--peer-min",
            "10",
            "--peer-max",
            "20",
            "--dcf-growth",
            "-0.05",
            "0.04",
            "0.10",
            "--discount-rate",
            "0.11",
        ]
    )

    assert args.command == "research"
    assert args.peer_min == 10
    assert args.peer_max == 20
    assert args.dcf_growth == [-0.05, 0.04, 0.10]
    assert args.discount_rate == pytest.approx(0.11)
