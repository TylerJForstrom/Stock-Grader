"""End-to-end checks of the planted-IC power-table harness.

A tiny grid is generated with the grader's OWN synthetic price module
(``stock_grader.data.synthetic``), scores are planted against the realized
forward-return ranks at a known blend, and each replication runs through the
production ``backtest`` CLI path exactly as ``scripts/power_table.py`` does for
the real calibration grid.  The assertions are the two facts that make a power
table trustworthy at all: the gate fires on an enormous planted signal, and it
stays quiet at the null roughly at its alpha.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from scripts.power_table import (
    REPO_ROOT,
    assert_outside_repo,
    evaluate_cell_frame,
    summarize_cell,
    write_artifacts,
)
from stock_grader.data.synthetic import generate_prices
from stock_grader.significance import norm_ppf

GATE_ALPHA = 0.05


def _planted_panel(
    *, n_names: int, n_months: int, rho: float, seed: int
) -> pd.DataFrame:
    """A frozen-score panel whose score/forward-return rank correlation is planted.

    Prices come from ``generate_prices`` (the repo's own synthetic generator);
    the score is ``rho * blom(forward-return rank) + sqrt(1-rho^2) * noise``,
    the same construction the Stock-Market-Sim calibration grid documents.
    """

    rng = np.random.default_rng(seed)
    n_days = 21 * (n_months + 3)
    closes = {}
    for index in range(n_names):
        ticker = f"SYN{index:03d}"
        prices = generate_prices(
            ticker, n_days=n_days, seed=seed * 100_003 + index, synthetic=True
        )
        closes[ticker] = prices["close"]
    close = pd.DataFrame(closes)
    month_end = close.groupby([close.index.year, close.index.month]).tail(1)
    month_end = month_end.iloc[-(n_months + 1) :]
    if len(month_end) != n_months + 1:
        raise AssertionError("not enough synthetic months generated")

    rows = []
    for position in range(n_months):
        signal_date = month_end.index[position]
        window_end = month_end.index[position + 1]
        forward = (
            month_end.iloc[position + 1] / month_end.iloc[position] - 1.0
        )
        ranks = forward.rank(method="average")
        blom = np.asarray(
            [norm_ppf((r - 0.375) / (n_names + 0.25)) for r in ranks]
        )
        noise = rng.standard_normal(n_names)
        scores = rho * blom + math.sqrt(1.0 - rho * rho) * noise
        for index, ticker in enumerate(month_end.columns):
            rows.append(
                {
                    "signal_date": signal_date,
                    "return_start": signal_date + pd.Timedelta(days=1),
                    "return_end": window_end,
                    "ticker": ticker,
                    "security_id": f"SID{index:05d}",
                    "score": float(scores[index]),
                    "forward_return": float(forward.iloc[index]),
                    "universe_id": "synthetic-test",
                    "universe_is_pit": True,
                    "return_is_total": True,
                    "delisting_return_included": True,
                }
            )
    return pd.DataFrame(rows)


def _stacked_replications(
    *, n_seeds: int, n_names: int, n_months: int, rho: float
) -> pd.DataFrame:
    frames = []
    for replication in range(n_seeds):
        frame = _planted_panel(
            n_names=n_names, n_months=n_months, rho=rho, seed=replication + 1
        )
        frame.insert(0, "seed", replication)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _cell(planted_ic: float, months: int, universe: int) -> dict:
    return {
        "file": f"test_ic{planted_ic}_m{months}_u{universe}.parquet",
        "sha256": "0" * 64,
        "planted_ic": planted_ic,
        "months": months,
        "universe": universe,
    }


def test_power_is_high_at_huge_planted_ic(tmp_path):
    """rho=0.95 over 12 periods x 60 names must be detected essentially always."""

    frame = _stacked_replications(n_seeds=5, n_names=60, n_months=12, rho=0.95)
    outcomes = evaluate_cell_frame(frame, tmp_path / "huge", "huge")
    result = summarize_cell(_cell(0.95, 12, 60), outcomes)
    assert result.n_seeds == 5
    assert result.gate_pass_rate >= 0.9
    # The evaluator must also recover the planted magnitude, not just the sign.
    assert result.mean_realized_rank_ic > 0.7


def test_false_positive_rate_near_alpha_at_null(tmp_path):
    """rho=0 replications must pass the gate at most ~alpha of the time."""

    frame = _stacked_replications(n_seeds=30, n_names=60, n_months=12, rho=0.0)
    outcomes = evaluate_cell_frame(frame, tmp_path / "null", "null")
    result = summarize_cell(_cell(0.0, 12, 60), outcomes)
    assert result.n_seeds == 30
    # The gate is a conjunction (DSR >= 0.95 AND CI low > 0), so its size is
    # at most alpha; allow binomial noise on 30 replications above that.
    assert result.gate_pass_rate <= GATE_ALPHA + 0.05
    assert abs(result.mean_realized_rank_ic) < 0.05


def test_scratch_inside_repo_is_refused(tmp_path):
    with pytest.raises(ValueError, match="inside the repository"):
        assert_outside_repo(REPO_ROOT / "build" / "anywhere", what="scratch")
    # And a location genuinely outside the checkout is accepted.
    assert_outside_repo(tmp_path, what="scratch")


def test_dated_artifacts_are_immutable(tmp_path):
    manifest = {
        "artifact": "test grid",
        "created_utc": "2026-01-01T00:00:00+00:00",
        "code_commit": "deadbeef",
        "synthetic_only": True,
    }
    frame = _stacked_replications(n_seeds=2, n_names=60, n_months=12, rho=0.0)
    outcomes = evaluate_cell_frame(frame, tmp_path / "im", "im")
    results = [summarize_cell(_cell(0.0, 12, 60), outcomes)]
    out_dir = tmp_path / "artifacts"
    written = write_artifacts(out_dir, "1999-01-01", manifest, results)
    assert len(written) == 3
    with pytest.raises(FileExistsError, match="immutable"):
        write_artifacts(out_dir, "1999-01-01", manifest, results)
