from __future__ import annotations

import pytest

from stock_grader.types import SectorClass
from stock_grader.valuation import (
    DCFScenario,
    build_valuation_analysis,
    equity_cash_flow_value,
    implied_growth_rate,
)
from test_pipeline import _universe


def test_equity_cash_flow_value_reconciles_and_reverse_dcf_recovers_growth():
    scenario = DCFScenario(
        name="base",
        growth_rate=0.06,
        discount_rate=0.10,
        terminal_growth_rate=0.025,
        years=5,
    )
    result = equity_cash_flow_value(
        base_fcf=1_000.0,
        shares_outstanding=100.0,
        scenario=scenario,
    )

    assert result.value_per_share == pytest.approx(
        result.present_value_explicit + result.present_value_terminal
    )
    recovered = implied_growth_rate(
        current_price=result.value_per_share,
        base_fcf=1_000.0,
        shares_outstanding=100.0,
        discount_rate=0.10,
        terminal_growth_rate=0.025,
        years=5,
    )
    assert recovered == pytest.approx(0.06, abs=1e-5)


def test_dcf_rejects_invalid_terminal_assumption():
    with pytest.raises(ValueError, match="greater than terminal"):
        equity_cash_flow_value(
            base_fcf=100.0,
            shares_outstanding=10.0,
            scenario=DCFScenario(
                name="bad",
                growth_rate=0.03,
                discount_rate=0.02,
                terminal_growth_rate=0.03,
            ),
        )


def test_business_model_gate_refuses_bank_dcf():
    snapshot = _universe(1)[0]
    snapshot.sector = SectorClass.BANK

    analysis = build_valuation_analysis(snapshot)

    assert not analysis.available
    assert not analysis.scenarios
    assert any("not appropriate" in warning for warning in analysis.warnings)


def test_snapshot_valuation_exposes_every_assumption():
    snapshot = _universe(1)[0]
    snapshot.price = 2.0

    analysis = build_valuation_analysis(
        snapshot,
        growth_rates=(-0.05, 0.03, 0.08),
        discount_rate=0.11,
        terminal_growth_rate=0.02,
    )

    assert analysis.available
    assert [item.scenario.name for item in analysis.scenarios] == ["bear", "base", "bull"]
    assert analysis.assumptions["growth_rates"] == [-0.05, 0.03, 0.08]
    assert analysis.assumptions["interpretation"] == "illustrative_scenarios_not_analyst_forecasts"


def test_public_float_lower_bound_is_omitted_from_dcf_comparisons():
    snapshot = _universe(1)[0]
    snapshot.price = None
    snapshot.meta["price_source"] = "public_float_lower_bound"
    snapshot.meta["price_lower_bound"] = 2.0
    snapshot.meta["valuation_price_rejected"] = "public_float_lower_bound"

    analysis = build_valuation_analysis(snapshot)

    assert analysis.available
    assert analysis.current_price is None
    assert analysis.assumptions["comparison_price_status"] == "lower_bound_omitted"
    assert any("public-float lower bound" in warning for warning in analysis.warnings)
