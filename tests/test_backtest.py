from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from stock_grader.backtest import (
    CAPACITY_ALLOWED_COLUMN,
    CAPACITY_TARGET_COLUMN,
    COST_COLUMN,
    BacktestConfig,
    _quantile_buckets,
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
    assert baseline.mean_rank_ic_net_side_aware is None
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
        assert period.rank_ic_net_side_aware is None

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
    assert report.mean_rank_ic_net_side_aware is not None
    # Costs uncorrelated with score can only add noise to the ranking, so the
    # net IC must sit below the gross IC of the same planted signal.
    assert report.mean_rank_ic_net_side_aware < report.mean_rank_ic
    for period in report.periods:
        assert period.rank_ic_net_side_aware is not None

    markdown = backtest_to_markdown(report)
    assert "Cost model" in markdown
    assert "Mean side-aware cost-net rank IC" in markdown


def test_the_cost_net_rank_ic_never_improves_when_the_expensive_names_score_low():
    """The regression for the sign of the cost on the SHORT leg.

    On a real small-cap panel the expensive names sit at the bottom of the
    score, which is the leg you are short. Subtracting a strictly positive cost
    from every name — the net return of a long — pushes those low-scoring names
    further down the ranking and mechanically RAISES the correlation, so the
    report says that charging honest costs improved the signal. Measured on the
    banded panels the "net" IC came out 119% LARGER than the gross one on the
    same run whose net spread was negative.

    The planted panel here reproduces that configuration exactly: cost is a
    decreasing function of score, corr(score, cost) is strongly negative, and
    the pre-fix single-signed statistic exceeds the gross IC. It must not.
    """
    panel = _panel()
    frames = []
    for _signal, group in panel.groupby("signal_date", sort=True):
        group = group.copy()
        # Cheap where the score is high, expensive where it is low: the
        # small-cap cost/score configuration, planted deterministically.
        ranks = group["score"].rank(pct=True)
        group[COST_COLUMN] = 400.0 - 390.0 * ranks
        frames.append(group)
    costed = pd.concat(frames, ignore_index=True)

    correlation = costed["score"].corr(costed[COST_COLUMN], method="spearman")
    assert correlation < -0.9, "the test panel must plant expensive names at low scores"

    report = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    assert report.mean_rank_ic_net_side_aware is not None
    assert report.mean_rank_ic_net_side_aware <= report.mean_rank_ic, (
        "a cost-net ranking statistic that beats the gross one is reporting that "
        "friction improved the signal"
    )

    # The two cost-net statistics in the same report must not point opposite
    # ways: costs this large destroy the spread, so the ranking statistic they
    # net out of cannot improve.
    gross_only = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0, cost_column=None)
    )
    assert report.mean_net_spread < gross_only.mean_gross_spread

    # And the arithmetic itself, spelled out: long half r - c, short half
    # r + c, middle bucket untouched.
    period = report.periods[0]
    first = costed.loc[costed["signal_date"] == costed["signal_date"].min()].copy()
    quintile = _quantile_buckets(first["score"], 5)
    side = np.sign(quintile.to_numpy(dtype="float64") - 2.0)
    expected = first["score"].corr(
        first["forward_return"] - side * first[COST_COLUMN] / 10_000.0,
        method="spearman",
    )
    assert period.rank_ic_net_side_aware == pytest.approx(float(expected), rel=1e-12)


def _capacity_panel(
    *, truncated_names: set[str], allowed_usd: float = 20_000.0, target_usd: float = 100_000.0
) -> pd.DataFrame:
    """The planted panel: some names the participation cap could barely fill."""
    panel = _panel()
    costed = panel.assign(**{COST_COLUMN: 20.0})
    truncated = costed["ticker"].isin(truncated_names)
    costed[CAPACITY_TARGET_COLUMN] = target_usd
    costed[CAPACITY_ALLOWED_COLUMN] = np.where(truncated, allowed_usd, target_usd)
    return costed


def test_a_capped_name_contributes_only_the_exposure_it_could_hold():
    """The regression for the capacity constraint being priced instead of applied.

    Before this, `estimate_cost` capped the notional at 1% of ADV20$, priced
    the CAPPED slice, and nothing downstream reduced the position: a name that
    could absorb $20k of a $100k order stayed a full-weight member of its
    quantile bucket while being charged what the $20k cost. On the banded
    panels that was 100% of band A's rows, so the headline band-A-vs-band-D
    comparison was measured between a name that was 79% unfilled and one that
    was fully filled.

    Here the top quintile's names are split: the ones the cap truncates carry a
    return nobody could have earned at full size, and the leg return must move
    away from it in proportion to the exposure that was actually available.
    """
    panel = _panel()
    first_date = panel["signal_date"].min()
    top_names = set(
        panel.loc[panel["signal_date"] == first_date]
        .sort_values("score")
        .tail(10)["ticker"]
    )
    # Truncate half of the top leg on every date (the tickers are stable).
    truncated = set(sorted(top_names)[:5])
    costed = _capacity_panel(truncated_names=truncated)

    config = BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    applied = evaluate_walk_forward(costed, config)
    priced_away = evaluate_walk_forward(
        costed,
        BacktestConfig(min_cross_section=30, bootstrap_samples=0, capacity_weighted=False),
    )

    assert applied.capacity_weighted is True
    assert priced_away.capacity_weighted is False
    assert applied.mean_deployable_fraction is not None
    assert applied.mean_deployable_fraction < 1.0
    # The unweighted run still REPORTS the shortfall, and says plainly that it
    # did not apply it.
    assert priced_away.mean_deployable_fraction == pytest.approx(
        applied.mean_deployable_fraction
    )
    assert any("priced away" in item or "switched off" in item for item in priced_away.limitations)
    assert any("per DEPLOYED dollar" in item for item in applied.limitations)

    # The weighted leg return is the exposure-weighted mean, not the equal
    # weight one, and the two differ. Checked against the arithmetic directly.
    period = applied.periods[0]
    unapplied = priced_away.periods[0]
    day = costed.loc[costed["signal_date"] == first_date].copy()
    buckets = _quantile_buckets(day["score"], 5)
    leg = day.loc[buckets == 4]
    weights = leg[CAPACITY_ALLOWED_COLUMN] / leg[CAPACITY_TARGET_COLUMN]
    expected = float((leg["forward_return"] * weights).sum() / weights.sum())
    assert period.top_return == pytest.approx(expected, rel=1e-12)
    assert period.top_return != pytest.approx(unapplied.top_return, rel=1e-9)
    assert unapplied.top_return == pytest.approx(float(leg["forward_return"].mean()), rel=1e-12)
    assert period.top_deployable_fraction == pytest.approx(float(weights.mean()), rel=1e-12)
    assert period.capacity_truncated_names == 5

    markdown = backtest_to_markdown(applied)
    assert "Capacity-weighted exposure | yes" in markdown
    assert "Mean deployable fraction of intended position" in markdown


def test_a_leg_the_cap_refuses_entirely_is_not_a_zero_return_period():
    """Nothing held earns no return, and "no return" is not "a return of zero"."""
    panel = _panel()
    first_date = panel["signal_date"].min()
    top_names = set(
        panel.loc[panel["signal_date"] == first_date]
        .sort_values("score")
        .tail(10)["ticker"]
    )
    costed = _capacity_panel(truncated_names=top_names, allowed_usd=0.0)
    report = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    # Some periods lose their whole top leg (the planted scores move a little
    # between dates), and those periods are rejected rather than booked at 0%.
    assert report.rejected_periods > 0
    for period in report.periods:
        assert math.isfinite(period.top_return)


def test_a_priced_row_without_a_position_size_refuses_rather_than_weighting_it_full():
    costed = _capacity_panel(truncated_names=set())
    costed.loc[0, CAPACITY_ALLOWED_COLUMN] = np.nan
    with pytest.raises(ValueError, match="unusable"):
        evaluate_walk_forward(costed, BacktestConfig(min_cross_section=30))


def test_an_untruncated_panel_is_unmoved_by_capacity_weighting():
    """Weighting by a column of ones must not perturb a single number.

    If it does, every panel whose cap never binds would have moved for a reason
    that has nothing to do with capacity.
    """
    costed = _capacity_panel(truncated_names=set())
    weighted = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    plain = evaluate_walk_forward(
        costed.drop(columns=[CAPACITY_TARGET_COLUMN, CAPACITY_ALLOWED_COLUMN]),
        BacktestConfig(min_cross_section=30, bootstrap_samples=0),
    )
    assert weighted.capacity_weighted is True
    assert weighted.mean_deployable_fraction == pytest.approx(1.0)
    assert weighted.mean_gross_spread == pytest.approx(plain.mean_gross_spread, rel=1e-12)
    assert weighted.mean_net_spread == pytest.approx(plain.mean_net_spread, rel=1e-12)
    for a, b in zip(weighted.periods, plain.periods, strict=True):
        assert a.top_return == pytest.approx(b.top_return, rel=1e-12)
        assert a.bottom_return == pytest.approx(b.bottom_return, rel=1e-12)
        assert a.top_cost_bps == pytest.approx(b.top_cost_bps, rel=1e-12)


def test_a_cost_column_without_capacity_columns_says_the_cap_was_not_applied():
    costed = _panel().assign(**{COST_COLUMN: 20.0})
    report = evaluate_walk_forward(
        costed, BacktestConfig(min_cross_section=30, bootstrap_samples=0)
    )
    assert report.capacity_weighted is False
    assert report.mean_deployable_fraction is None
    assert any("no cost_notional_target_usd" in item for item in report.limitations)


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
