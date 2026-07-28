from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_grader.backtest import (
    BacktestConfig,
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
        for index in range(names):
            rows.append(
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
