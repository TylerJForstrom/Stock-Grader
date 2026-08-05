from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_grader.backtest import (
    COST_COLUMN,
    BacktestConfig,
    _validate_panel,
    backtest_to_markdown,
    evaluate_walk_forward,
    purged_walk_forward_splits,
)


def _panel(periods: int = 18, names: int = 50) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    for period, signal in enumerate(pd.date_range("2022-01-31", periods=periods, freq="ME")):
        scores = rng.normal(size=names)
        returns = 0.04 * scores + rng.normal(scale=0.03, size=names)
        start = signal + pd.Timedelta(days=1)
        end = signal + pd.offsets.MonthEnd(1)
        rows.extend(
            {
                "signal_date": signal,
                "filed_through": signal,
                "return_start": start,
                "return_end": end,
                "ticker": f"T{index:03d}",
                "cik": f"{index + 1:010d}",
                "score": scores[index],
                "forward_return": returns[index],
                "universe_is_pit": True,
                "return_is_total": True,
                "delisting_return_included": True,
            }
            for index in range(names)
        )
    return pd.DataFrame(rows)


def test_walk_forward_detects_planted_out_of_sample_ordering():
    report = evaluate_walk_forward(
        _panel(),
        BacktestConfig(
            min_cross_section=30,
            bootstrap_samples=100,
            transaction_cost_bps=5,
        ),
    )

    assert report.mean_rank_ic > 0.5
    assert report.mean_net_spread > 0
    assert report.spread_positive_rate > 0.8
    assert report.quantile_monotonicity > 0.8
    assert report.rank_ic_interval is not None
    assert report.net_spread_interval is not None
    assert report.observations == 18 * 50
    assert all(report.input_contract.values())
    assert report.periods[0].top_turnover == 1.0
    assert report.periods[0].bottom_turnover == 1.0

    markdown = backtest_to_markdown(report)
    assert "Walk-forward score validation" in markdown
    assert "not a forecast" in markdown
    assert "Moving-block 95% interval" in markdown


def test_walk_forward_rejects_filing_and_return_leakage():
    panel = _panel(3, 20)
    panel.loc[0, "filed_through"] = panel.loc[0, "signal_date"] + pd.Timedelta(days=1)
    with pytest.raises(ValueError, match="filings after"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))

    panel = _panel(3, 20)
    panel["return_start"] = panel["signal_date"]
    with pytest.raises(ValueError, match="strictly after"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))


def test_walk_forward_rejects_duplicate_security_date():
    panel = _panel(3, 20)
    panel = pd.concat([panel, panel.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))


def test_validate_panel_rejects_mixed_universe_ids_unless_explicitly_allowed():
    panel = _panel(3, 20)
    panel["universe_id"] = "liq1000_v1"
    first_date = panel["signal_date"].min()
    # Missing legacy IDs must count as a distinct value. pandas nunique() defaults to dropping
    # nulls, which would otherwise let a narrow+wide concatenation pass this guard.
    panel.loc[panel["signal_date"] == first_date, "universe_id"] = pd.NA

    with pytest.raises(ValueError, match="mixes multiple universe_id"):
        _validate_panel(panel)
    allowed = _validate_panel(panel, allow_mixed_universes=True)
    assert len(allowed) == len(panel)

    with pytest.raises(ValueError, match="mixes multiple universe_id"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))
    report = evaluate_walk_forward(
        panel,
        BacktestConfig(min_cross_section=20, bootstrap_samples=0),
        allow_mixed_universes=True,
    )
    assert report.input_contract["single_universe_id"] is False
    assert any("mixes universe_id" in limitation for limitation in report.limitations)

    panel["universe_id"] = "liq1000_v1"
    assert len(_validate_panel(panel)) == len(panel)


def test_turnover_tracks_permanent_ids_across_ticker_changes():
    panel = _panel(2, 20)
    second_date = panel["signal_date"].max()
    panel.loc[panel["signal_date"] == second_date, "ticker"] = (
        panel.loc[panel["signal_date"] == second_date, "ticker"] + "_NEW"
    )
    # Make both dates' score/return order identical so portfolio membership is unchanged.
    first = panel.loc[panel["signal_date"] == panel["signal_date"].min()].sort_values("cik")
    second_index = panel.loc[panel["signal_date"] == second_date].sort_values("cik").index
    panel.loc[second_index, "score"] = first["score"].to_numpy()
    panel.loc[second_index, "forward_return"] = first["forward_return"].to_numpy()

    report = evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))

    assert report.periods[0].top_turnover == 1.0
    assert report.periods[1].top_turnover == 0.0
    assert report.periods[1].bottom_turnover == 0.0


def test_total_loss_is_valid_but_below_minus_one_is_not():
    panel = _panel(3, 20)
    panel.loc[0, "forward_return"] = -1.0
    report = evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))
    assert report.observations == 60

    panel.loc[0, "forward_return"] = -1.0001
    with pytest.raises(ValueError, match="below -100%"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=20))


def test_purged_splits_have_strict_chronology_and_embargo():
    dates = pd.date_range("2020-01-31", periods=20, freq="ME")
    splits = list(
        purged_walk_forward_splits(
            dates,
            train_periods=8,
            embargo_periods=2,
            test_periods=3,
        )
    )

    assert splits
    for split in splits:
        assert max(split.train_dates) < min(split.embargo_dates)
        assert max(split.embargo_dates) < min(split.test_dates)
        assert not set(split.train_dates) & set(split.test_dates)


# -- per-row transaction costs -------------------------------------------------
#
# The evaluator's charge used to be one number for every name in every band.
# That single assumption is the reason a small-cap "edge" can be an artifact:
# a flat rate undercharges the thin names and overcharges the liquid ones, in
# exactly the direction that manufactures the result the thin side is hoping
# for. These tests pin the replacement AND pin that nothing moved for a panel
# that carries no cost column.


def _flat_report(panel: pd.DataFrame, **kwargs) -> object:
    return evaluate_walk_forward(
        panel,
        BacktestConfig(min_cross_section=30, bootstrap_samples=50, **kwargs),
    )


def _numeric_fields(report) -> dict:
    """Every number an old panel's evaluation ever produced."""
    return {
        "mean_rank_ic": report.mean_rank_ic,
        "rank_ic_information_ratio": report.rank_ic_information_ratio,
        "rank_ic_positive_rate": report.rank_ic_positive_rate,
        "mean_gross_spread": report.mean_gross_spread,
        "mean_net_spread": report.mean_net_spread,
        "spread_positive_rate": report.spread_positive_rate,
        "annualized_net_spread": report.annualized_net_spread,
        "annualized_spread_sharpe": report.annualized_spread_sharpe,
        "max_drawdown": report.max_drawdown,
        "mean_turnover": report.mean_turnover,
        "quantile_monotonicity": report.quantile_monotonicity,
        "rank_ic_interval": report.rank_ic_interval,
        "net_spread_interval": report.net_spread_interval,
        "observations": report.observations,
        "rejected_periods": report.rejected_periods,
        "periods": [
            (
                item.signal_date,
                item.rank_ic,
                item.top_return,
                item.bottom_return,
                item.gross_spread,
                item.net_spread,
                item.top_turnover,
                item.bottom_turnover,
                tuple(item.quantile_returns),
            )
            for item in report.periods
        ],
    }


def test_a_panel_without_a_cost_column_evaluates_exactly_as_it_always_did():
    """The regression that protects every result recorded before this existed.

    Not "approximately the same" and not "the same headline number": every
    numeric field of the report, including per-period returns and both bootstrap
    intervals, must be identical to what the flat charge produced.
    """
    panel = _panel()
    assert COST_COLUMN not in panel.columns
    baseline = _flat_report(panel, transaction_cost_bps=10.0)
    assert baseline.per_row_costs_used is False
    assert baseline.mean_rank_ic_net is None
    assert baseline.mean_round_trip_cost_bps is None
    assert baseline.no_cost_estimate_rows == 0

    # The flat arithmetic, spelled out independently of the implementation.
    for period in baseline.periods:
        expected = period.gross_spread - (10.0 / 10_000.0) * (
            period.top_turnover + period.bottom_turnover
        )
        assert period.net_spread == pytest.approx(expected, rel=0, abs=1e-15)
        assert period.top_cost_bps == 10.0
        assert period.bottom_cost_bps == 10.0
        assert period.rank_ic_net is None

    # And nothing about the report moved when the cost machinery was added:
    # forcing the flat path explicitly must reproduce it field for field.
    forced = _flat_report(panel, transaction_cost_bps=10.0, cost_column=None)
    assert _numeric_fields(forced) == _numeric_fields(baseline)

    markdown = backtest_to_markdown(baseline)
    assert "Cost model" not in markdown
    assert "fixed turnover charge" in markdown


def test_a_constant_cost_column_reproduces_the_flat_charge_it_replaces():
    """The bridge between the two paths: a column of 10s IS the flat 10 bps.

    If this fails, the per-row charge is not a generalisation of the flat one
    and no result computed under either can be compared to the other.
    """
    panel = _panel()
    flat = _flat_report(panel, transaction_cost_bps=10.0)
    with_column = _flat_report(
        panel.assign(**{COST_COLUMN: 10.0}), transaction_cost_bps=10.0
    )
    assert with_column.per_row_costs_used is True
    assert with_column.mean_round_trip_cost_bps == pytest.approx(10.0)
    # Everything the cost cannot touch is identical; the cost-bearing numbers
    # agree to floating-point noise. They are not bit-identical on purpose:
    # the flat path keeps its original single-product expression so that old
    # results do not move, and `rate*(a+b)` differs from `rate*a + rate*b` in
    # the last bit. Pinning them to 1e-12 says the two paths are the same
    # model without pretending they are the same instruction sequence.
    assert with_column.mean_rank_ic == flat.mean_rank_ic
    assert with_column.mean_gross_spread == flat.mean_gross_spread
    assert with_column.mean_turnover == flat.mean_turnover
    assert with_column.observations == flat.observations
    assert with_column.mean_net_spread == pytest.approx(flat.mean_net_spread, rel=1e-12)
    assert len(with_column.periods) == len(flat.periods)
    for priced, plain in zip(with_column.periods, flat.periods, strict=True):
        assert priced.signal_date == plain.signal_date
        assert priced.top_return == plain.top_return
        assert priced.bottom_return == plain.bottom_return
        assert priced.top_turnover == plain.top_turnover
        assert priced.bottom_turnover == plain.bottom_turnover
        assert priced.net_spread == pytest.approx(plain.net_spread, rel=1e-12)


def test_per_row_costs_charge_each_leg_what_its_own_names_cost():
    """A cheap top leg and an expensive bottom leg must not average away.

    The planted panel scores every name, then charges the highest-scoring
    quintile 4 bps and the lowest 200 bps. Under the flat charge the two legs
    are indistinguishable; under per-row costs the spread must fall by the
    difference, and by exactly the difference.
    """
    panel = _panel()
    per_period_costs = []
    frames = []
    for _signal, group in panel.groupby("signal_date", sort=True):
        ordered = group.sort_values("score")
        cheap = set(ordered.tail(10)["ticker"])
        dear = set(ordered.head(10)["ticker"])
        group = group.copy()
        group[COST_COLUMN] = [
            4.0 if ticker in cheap else (200.0 if ticker in dear else 50.0)
            for ticker in group["ticker"]
        ]
        frames.append(group)
        per_period_costs.append((cheap, dear))
    costed = pd.concat(frames, ignore_index=True)

    report = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    assert report.per_row_costs_used is True
    for period in report.periods:
        # quintiles of 50 names are 10 wide, so the legs are exactly the
        # planted sets and the leg costs are exactly the planted rates.
        assert period.top_cost_bps == pytest.approx(4.0)
        assert period.bottom_cost_bps == pytest.approx(200.0)
        expected = period.gross_spread - (
            0.0004 * period.top_turnover + 0.02 * period.bottom_turnover
        )
        assert period.net_spread == pytest.approx(expected, rel=0, abs=1e-15)

    # The expensive short leg is a real charge, so the net spread must be worse
    # than the flat 10 bps would have made it look.
    flat = _flat_report(costed, transaction_cost_bps=10.0, cost_column=None)
    assert report.mean_net_spread < flat.mean_net_spread


def test_cost_net_rank_ic_is_reported_only_when_costs_actually_vary_by_name():
    panel = _panel()
    varied = panel.copy()
    rng = np.random.default_rng(3)
    varied[COST_COLUMN] = rng.uniform(5.0, 400.0, size=len(varied))
    report = evaluate_walk_forward(
        varied, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    assert report.mean_rank_ic_net is not None
    # Costs uncorrelated with score can only add noise to the ranking, so the
    # net IC must sit below the gross IC of the same planted signal.
    assert report.mean_rank_ic_net < report.mean_rank_ic
    for period in report.periods:
        assert period.rank_ic_net is not None

    markdown = backtest_to_markdown(report)
    assert "Cost model" in markdown
    assert "Mean cost-net rank IC" in markdown


def test_rows_with_no_cost_estimate_are_dropped_and_counted_not_back_filled():
    """§ the short-window refusal: a name whose liquidity cannot be measured is
    a name you cannot honestly claim to have traded."""
    panel = _panel()
    costed = panel.assign(**{COST_COLUMN: 20.0})
    unpriceable = costed["ticker"].isin({"T000", "T001", "T002"})
    costed.loc[unpriceable, COST_COLUMN] = np.nan

    report = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    assert report.no_cost_estimate_rows == int(unpriceable.sum())
    assert report.observations == len(panel) - int(unpriceable.sum())
    assert all(period.no_cost_estimate_dropped == 3 for period in report.periods)
    assert any("no cost estimate" in item for item in report.limitations)

    # Not substituted: the surviving names' costs are unchanged, so the mean is
    # the planted 20 rather than a blend with a filled-in default.
    assert report.mean_round_trip_cost_bps == pytest.approx(20.0)


def test_a_panel_that_can_price_nothing_refuses_rather_than_evaluating_gross():
    panel = _panel().assign(**{COST_COLUMN: np.nan})
    with pytest.raises(ValueError, match="can price none of them"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=30))


def test_a_negative_cost_is_a_defect_not_a_rebate():
    panel = _panel().assign(**{COST_COLUMN: 20.0})
    panel.loc[0, COST_COLUMN] = -5.0
    with pytest.raises(ValueError, match="cannot be negative"):
        evaluate_walk_forward(panel, BacktestConfig(min_cross_section=30))


#: The flat-cost report this evaluator produced BEFORE per-row costs existed,
#: on the fixed panel above, transcribed from a run of the pre-change module at
#: origin/main. Literals, not a re-run of the current code against itself: a
#: self-comparison proves the two branches agree with each other and says
#: nothing about whether either still agrees with what was published.
PRE_COST_MODEL_FLAT_REPORT = {
    "mean_rank_ic": 0.7808350006669333,
    "mean_gross_spread": 0.10923404676648148,
    "mean_net_spread": 0.10764515787759261,
    "annualized_net_spread": 2.4044124704655285,
    "annualized_spread_sharpe": 19.058712573113795,
    "max_drawdown": 0.0,
    "mean_turnover": 0.7944444444444445,
    "quantile_monotonicity": 0.9999999999999999,
    "rank_ic_information_ratio": 47.47140570972663,
    "rank_ic_positive_rate": 1.0,
    "spread_positive_rate": 1.0,
    "rank_ic_interval": (0.757234627184207, 0.7975256769374416),
    "net_spread_interval": (0.10118226521735488, 0.11595007878140037),
    "observations": 900,
    "rejected_periods": 0,
}

PRE_COST_MODEL_NET_SPREADS = [
    0.08490999667821411,
    0.09115119641720172,
    0.11843506333422667,
    0.10141303571531636,
    0.10180340445472932,
    0.08787876779271844,
    0.10809666155705312,
    0.09370343227163921,
    0.1261012193193013,
    0.11176226300386793,
    0.14545775133402045,
    0.09967867408961187,
    0.10333083622974767,
    0.1442513548587573,
    0.10867884879009088,
    0.12312582617774509,
    0.0702839620548265,
    0.117550547717599,
]


def test_the_flat_path_still_produces_the_numbers_it_produced_before_costs_existed():
    """Bit-for-bit, against literals taken from the pre-change implementation.

    Every result recorded under the flat charge — including anything already
    written into an append-only ledger — is only reproducible if this holds.
    """
    report = _flat_report(_panel(), transaction_cost_bps=10.0)
    for field, expected in PRE_COST_MODEL_FLAT_REPORT.items():
        assert getattr(report, field) == expected, field
    assert [item.net_spread for item in report.periods] == PRE_COST_MODEL_NET_SPREADS
    # The two original limitation lines are still the first two, verbatim.
    assert report.limitations[:2] == [
        (
            "Transaction costs are a fixed turnover charge and do not model market "
            "impact or borrow."
        ),
        (
            "Bootstrap intervals describe historical period variability, not "
            "future-return certainty."
        ),
    ]
