"""Focused regressions for ranking, sensitivity, and nonlinear explanation semantics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_grader.aggregate import aggregate
from stock_grader.pipeline import GradeConfig, _sensitivity_range_and_letter_frequencies
from stock_grader.scoring import (
    PERCENTILE_CUTOFFS,
    apply_hysteresis,
    explain_aggregate_contributions,
    hazen_percentile,
    to_letter,
    uncertainty_interval,
)


def test_hazen_percentile_is_centered_and_tie_aware():
    assert hazen_percentile(100.0, [100.0]) == pytest.approx(50.0)
    assert hazen_percentile(1.0, [2.0]) == pytest.approx(25.0)
    assert hazen_percentile(2.0, [1.0]) == pytest.approx(75.0)
    assert hazen_percentile(5.0, [5.0, 5.0, 5.0]) == pytest.approx(50.0)


def test_hysteresis_uses_both_boundaries_and_the_active_curve():
    assert apply_hysteresis(72.2, "B", band=1.0) == "B+"
    assert apply_hysteresis(72.2, "B", band=1.5) == "B"
    assert apply_hysteresis(64.2, "B", band=1.0) == "B"
    assert apply_hysteresis(63.8, "B", band=1.0) == "B-"

    # On the cross-sectional curve, B begins at the 70th percentile and B+ at the 80th.
    assert apply_hysteresis(80.5, "B", cutoffs=PERCENTILE_CUTOFFS) == "B"
    assert apply_hysteresis(81.5, "B", cutoffs=PERCENTILE_CUTOFFS) == "B+"


def test_cross_sectional_scenarios_use_the_percentile_letter_curve():
    sensitivity, frequencies = _sensitivity_range_and_letter_frequencies(
        np.array([100.0]),
        np.array([0.0]),
        GradeConfig(curve="cross_sectional"),
    )
    # Hazen percentile is 75 in a two-security universe. Its cross-sectional letter is B,
    # whereas the absolute 75-point curve would incorrectly call it B+.
    assert sensitivity == pytest.approx((75.0, 75.0))
    assert frequencies == {"B": 1.0}
    assert to_letter(75.0) == "B+"


def test_ces_attributions_exactly_reconcile_the_nonlinear_composite():
    scores = pd.Series({"strong": 90.0, "weak": 30.0})
    weights = pd.Series({"strong": 0.5, "weak": 0.5})
    composite = aggregate(scores, weights, method="ces", rho=0.5)
    contributions = explain_aggregate_contributions(
        scores,
        weights,
        aggregator="ces",
        rho=0.5,
    )

    assert composite is not None
    assert 50.0 + sum(contributions.values()) == pytest.approx(composite, abs=1e-10)
    assert composite != pytest.approx(float((scores * weights).sum()))


def test_one_component_sensitivity_does_not_claim_zero_width():
    samples = uncertainty_interval(
        pd.Series({"only": 62.0}),
        pd.Series({"only": 1.0}),
        coverage=0.4,
        draws=101,
        return_samples=True,
    )
    assert isinstance(samples, np.ndarray)
    assert float(np.ptp(samples)) > 0.0
    assert float(np.median(samples)) == pytest.approx(62.0)
