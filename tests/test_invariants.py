"""Property tests for the scoring machinery.

These assert the things that must hold for *any* input, which is where the subtle bugs live. Every
test here corresponds to a failure mode that would otherwise produce a confident, wrong grade.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_grader import aggregate as agg
from stock_grader import normalize as norm
from stock_grader import weighting as wt
from stock_grader.registry import AGGREGATORS, NORMALIZERS, WEIGHTINGS
from stock_grader.scoring import to_letter, uncertainty_interval, explain_contributions

CROSS_SECTIONAL = [n for n in NORMALIZERS.names() if n not in ("piecewise", "double_sigmoid")]


@pytest.fixture
def panel() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    base = rng.standard_normal((80, 3))
    return pd.DataFrame(
        {
            "pe": base[:, 0],
            "ps": base[:, 0] + 0.05 * rng.standard_normal(80),  # near-duplicate on purpose
            "roe": base[:, 1],
            "growth": base[:, 2],
            "debt": rng.standard_normal(80),
        }
    )


# ---------------------------------------------------------------------------- normalizers


@pytest.mark.parametrize("method", CROSS_SECTIONAL)
def test_normalizer_preserves_nan(method):
    """A missing input must stay missing.

    If a normalizer turned NaN into a neutral 50, a metric absent for every company would cast a
    full-weight 'perfectly average' vote instead of being renormalised away.
    """
    values = pd.Series([1.0, np.nan, 3.0, np.nan, 5.0])
    scored = NORMALIZERS.get(method)(values)
    assert scored.isna().tolist() == values.isna().tolist()


@pytest.mark.parametrize("method", CROSS_SECTIONAL)
def test_normalizer_in_range(method):
    values = pd.Series(np.random.default_rng(1).standard_normal(50) * 100)
    scored = NORMALIZERS.get(method)(values).dropna()
    assert scored.between(0.0, 100.0).all()


@pytest.mark.parametrize("method", CROSS_SECTIONAL)
def test_normalizer_handles_degenerate(method):
    for values in (
        pd.Series([5.0] * 6),  # zero variance
        pd.Series([np.nan] * 4),
        pd.Series([1.0]),
        pd.Series([], dtype="float64"),
    ):
        result = NORMALIZERS.get(method)(values)
        assert len(result) == len(values)


@pytest.mark.parametrize("method", ["zscore", "robust_z", "percentile", "gaussian_rank"])
def test_normalizer_is_monotone(method):
    """A higher raw value must never receive a lower score."""
    values = pd.Series([1.0, 2.0, 3.0, 10.0, 50.0])
    scored = NORMALIZERS.get(method)(values)
    assert scored.is_monotonic_increasing


def test_direction_flips_scores():
    values = pd.Series([10.0, 20.0, 30.0])
    up = norm.normalize_series(values, direction=1)
    down = norm.normalize_series(values, direction=-1)
    assert up.is_monotonic_increasing
    assert down.is_monotonic_decreasing


def test_non_monotonic_band_penalises_both_extremes():
    """An ideal-band metric must score the middle above either end.

    Without this, a 95% dividend payout ratio ranks as the best income stock in the universe.
    """
    values = pd.Series({"none": 0.0, "ideal": 0.45, "extreme": 0.98})
    scored = norm.normalize_series(values, direction=0, ideal_band=(0.30, 0.60))
    assert scored["ideal"] > scored["none"]
    assert scored["ideal"] > scored["extreme"]


def test_piecewise_works_without_a_universe():
    anchors = [(-0.2, 0.0), (0.0, 20.0), (0.2, 75.0), (0.8, 100.0)]
    good = norm.normalize_series(pd.Series({"X": 0.30}), anchors=anchors, direction=1)
    bad = norm.normalize_series(pd.Series({"X": -0.10}), anchors=anchors, direction=1)
    assert good.iloc[0] > 70
    assert bad.iloc[0] < 30


# ---------------------------------------------------------------------------- aggregators


@pytest.mark.parametrize("method", AGGREGATORS.names())
def test_aggregator_renormalizes_missing_weights(method):
    """Missing metrics must not drag a pillar toward zero.

    Two metrics at 80 and 60 with two more missing is a 70, not a 35.
    """
    scores = pd.Series({"a": 80.0, "b": 60.0, "c": np.nan, "d": np.nan})
    weights = pd.Series({"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25})
    result = AGGREGATORS.get(method)(scores, weights)
    assert result is not None
    assert 55.0 <= result <= 85.0


@pytest.mark.parametrize("method", AGGREGATORS.names())
def test_aggregator_returns_none_on_empty(method):
    assert AGGREGATORS.get(method)(pd.Series([np.nan] * 3), None) is None
    assert AGGREGATORS.get(method)(pd.Series([], dtype="float64"), None) is None


@pytest.mark.parametrize("method", AGGREGATORS.names())
def test_aggregator_order_invariant(method):
    """Permuting the metrics must not change the result."""
    scores = pd.Series({"a": 80.0, "b": 55.0, "c": 30.0, "d": 91.0})
    weights = pd.Series({"a": 0.4, "b": 0.3, "c": 0.2, "d": 0.1})
    shuffled = list("dbca")
    first = AGGREGATORS.get(method)(scores, weights)
    second = AGGREGATORS.get(method)(scores[shuffled], weights[shuffled])
    assert first == pytest.approx(second, abs=1e-9)


@pytest.mark.parametrize("method", ["weighted_mean", "ces", "geometric_mean", "harmonic_mean"])
def test_aggregator_is_monotone(method):
    """Improving any single metric must never lower the aggregate."""
    base = pd.Series({"a": 50.0, "b": 50.0, "c": 50.0})
    better = pd.Series({"a": 70.0, "b": 50.0, "c": 50.0})
    assert AGGREGATORS.get(method)(better, None) >= AGGREGATORS.get(method)(base, None)


def test_ces_rho_controls_compensation():
    """The compensation dial must actually dial.

    Balanced and lopsided profiles share an arithmetic mean; every lower rho must separate them
    more than the one above it.
    """
    balanced = pd.Series([50.0] * 4)
    lopsided = pd.Series([100.0, 100.0, 0.0, 0.0])
    gaps = [
        agg.ces(balanced, None, rho=rho) - agg.ces(lopsided, None, rho=rho)
        for rho in (1.0, 0.5, 0.0)
    ]
    assert gaps[0] == pytest.approx(0.0, abs=1e-6)
    assert gaps[1] > gaps[0]
    assert gaps[2] > gaps[1]


# ---------------------------------------------------------------------------- weighting


@pytest.mark.parametrize("method", WEIGHTINGS.names())
def test_weights_satisfy_contract(method, panel):
    """Every method: non-negative, finite, sums to one, indexed by the components."""
    ctx = wt.WeightingContext(
        forward_returns=pd.Series(np.random.default_rng(2).standard_normal(len(panel)), index=panel.index)
    )
    weights = wt.compute_weights(panel, method=method, ctx=ctx)
    assert list(weights.index) == list(panel.columns)
    assert weights.sum() == pytest.approx(1.0, abs=1e-9)
    assert (weights >= 0).all()
    assert np.isfinite(weights).all()


@pytest.mark.parametrize("method", WEIGHTINGS.names())
def test_weights_degrade_gracefully_for_one_security(method):
    """A single-stock grade gives a one-row panel; nothing may raise or emit garbage."""
    single = pd.DataFrame({"a": [1.0], "b": [2.0], "c": [3.0]})
    ctx = wt.WeightingContext()
    weights = wt.compute_weights(single, method=method, ctx=ctx)
    assert weights.sum() == pytest.approx(1.0, abs=1e-9)
    assert (weights >= 0).all()


@pytest.mark.parametrize("method", WEIGHTINGS.names())
def test_weights_survive_a_constant_column(method, panel):
    """A metric identical across the universe must not break covariance or entropy methods."""
    panel = panel.copy()
    panel["constant"] = 1.0
    weights = wt.compute_weights(panel, method=method, ctx=wt.WeightingContext())
    assert weights.sum() == pytest.approx(1.0, abs=1e-9)
    assert np.isfinite(weights).all()


@pytest.mark.parametrize("method", ["hrp", "decorrelated", "critic"])
def test_redundancy_aware_methods_discount_duplicates(method, panel):
    """Three views of one factor should not get three votes.

    ``pe`` and ``ps`` are near-identical in the fixture, so a redundancy-aware method must give
    them less combined weight than equal weighting would.
    """
    weights = wt.compute_weights(panel, method=method, ctx=wt.WeightingContext())
    equal = wt.compute_weights(panel, method="equal", ctx=wt.WeightingContext())
    assert weights[["pe", "ps"]].sum() < equal[["pe", "ps"]].sum()


def test_supervised_methods_recover_a_planted_signal(panel):
    """A weighting method that cannot find a planted signal is broken.

    ``roe`` is constructed to drive returns; every supervised method must weight it above the
    factor that is pure noise.
    """
    returns = 2.0 * panel["roe"] + 0.25 * np.random.default_rng(3).standard_normal(len(panel))
    ctx = wt.WeightingContext(forward_returns=returns)
    for method in ("ic", "regression", "shapley"):
        weights = wt.compute_weights(panel, method=method, ctx=wt.WeightingContext(forward_returns=returns))
        assert weights["roe"] > weights["debt"], f"{method} failed to find the planted signal"


def test_supervised_method_falls_back_without_returns(panel):
    ctx = wt.WeightingContext()
    weights = wt.compute_weights(panel, method="ic", ctx=ctx)
    assert weights.sum() == pytest.approx(1.0)
    assert any("forward returns" in w for w in ctx.warnings)


def test_ahp_flags_inconsistent_judgements():
    """Incoherent pairwise judgements must be reported, not silently averaged."""
    columns = ["a", "b", "c"]
    # a>b, b>c, but c>a — a cycle.
    matrix = pd.DataFrame(
        [[1.0, 5.0, 1 / 5], [1 / 5, 1.0, 5.0], [5.0, 1 / 5, 1.0]], index=columns, columns=columns
    )
    ctx = wt.WeightingContext(pairwise=matrix)
    weights = wt.compute_weights(pd.DataFrame(columns=columns, data=[[1.0, 2.0, 3.0]]),
                                 method="ahp", ctx=ctx)
    assert weights.sum() == pytest.approx(1.0)
    assert any("inconsistent" in w for w in ctx.warnings)


# ---------------------------------------------------------------------------- scoring


def test_grade_scale_is_monotone():
    letters = [to_letter(s) for s in range(0, 101, 5)]
    order = ["F", "D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+"]
    positions = [order.index(letter) for letter in letters]
    assert positions == sorted(positions)


def test_uncertainty_widens_with_disagreement():
    weights = pd.Series({f"m{i}": 1 / 6 for i in range(6)})
    agree = pd.Series({f"m{i}": 70.0 for i in range(6)})
    disagree = pd.Series({f"m{i}": (95.0 if i % 2 else 45.0) for i in range(6)})
    tight = uncertainty_interval(agree, weights, seed=1)
    wide = uncertainty_interval(disagree, weights, seed=1)
    assert (wide[1] - wide[0]) > (tight[1] - tight[0])


def test_uncertainty_widens_as_coverage_falls():
    scores = pd.Series({f"m{i}": 60.0 + i for i in range(8)})
    weights = pd.Series({f"m{i}": 1 / 8 for i in range(8)})
    full = uncertainty_interval(scores, weights, coverage=1.0, seed=1)
    sparse = uncertainty_interval(scores, weights, coverage=0.5, seed=1)
    assert (sparse[1] - sparse[0]) > (full[1] - full[0])


def test_contributions_reconstruct_the_score():
    """Contributions must be an exact decomposition, not an approximation."""
    scores = pd.Series({"a": 88.0, "b": 22.0, "c": 61.0, "d": 47.0})
    weights = pd.Series({"a": 0.4, "b": 0.3, "c": 0.2, "d": 0.1})
    contributions = explain_contributions(scores, weights)
    assert 50.0 + sum(contributions.values()) == pytest.approx(float((scores * weights).sum()))


def test_deterministic_under_a_fixed_seed():
    scores = pd.Series({f"m{i}": float(50 + i) for i in range(10)})
    weights = pd.Series({f"m{i}": 0.1 for i in range(10)})
    first = uncertainty_interval(scores, weights, seed=7)
    second = uncertainty_interval(scores, weights, seed=7)
    assert first == second
