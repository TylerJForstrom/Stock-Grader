"""Tests for SEC-derived pricing.

The measured facts these pin down, all from live SEC data on 2026-07-24:

* Insider-transaction median prices landed inside the known market range for all eight test
  companies (AAPL 227.63, WMT 98.02, JPM 297.94, NVDA 174.88 for 2025Q3).
* 8.5% of raw transaction rows carried implausible dates — the 2026q1 bundle contained dates from
  2002 to 2027, and a future-dated row silently wins every "latest price" query.
* Public float understates price by exactly the affiliate holding: -50% for Walmart, -37% for
  Simon Property, under 3% for the six widely-held names.
* Calibrating the affiliate fraction against a price from a *different date* is circular and
  silently reproduces its own input.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_grader.data.sec_prices import (
    MARKET_PRICED_CODES,
    _quarter_bounds,
    calibrate_non_affiliate_fraction,
    calibrated_price_from_float,
    implied_price_from_float,
    resolve_price,
)


class TestTransactionCodes:
    def test_only_market_priced_codes_are_used(self):
        """Option exercises transact at the strike, not the market.

        Including code M would drag a price estimate toward strikes set years earlier; A (grant)
        and G (gift) are frequently recorded at zero.
        """
        assert MARKET_PRICED_CODES == {"S", "P", "F"}
        for code in ("M", "A", "G", "D", "J"):
            assert code not in MARKET_PRICED_CODES


class TestQuarterBounds:
    def test_bounds_bracket_the_quarter_with_slack(self):
        low, high = _quarter_bounds("2026q1")
        assert low < date(2026, 1, 1)
        assert high > date(2026, 3, 31)

    @pytest.mark.parametrize("quarter", ["2024q1", "2024q2", "2024q3", "2024q4"])
    def test_every_quarter_parses(self, quarter):
        low, high = _quarter_bounds(quarter)
        assert low < high

    def test_a_future_dated_row_falls_outside_its_bundle(self):
        """The real 2026q1 bundle contained rows dated 2027 and 2002."""
        low, high = _quarter_bounds("2026q1")
        assert not (low <= date(2027, 1, 25) <= high)
        assert not (low <= date(2002, 2, 24) <= high)


class TestImpliedPrice:
    def test_uncorrected_float_is_a_lower_bound(self):
        """Public float excludes affiliates, so dividing by *all* shares can only understate."""
        price = implied_price_from_float(50e9, 1e9)
        assert price == pytest.approx(50.0)

    def test_affiliate_correction_raises_the_price(self):
        """Walmart's real case: half the shares are affiliate-held, so the naive price halves."""
        naive = implied_price_from_float(391.7e9, 8.016e9)
        corrected = implied_price_from_float(391.7e9, 8.016e9, non_affiliate_fraction=0.499)
        assert naive == pytest.approx(48.9, abs=0.5)
        assert corrected == pytest.approx(98.0, abs=1.0)
        assert corrected > naive

    @pytest.mark.parametrize("bad", [None, 0, -1e9])
    def test_degenerate_inputs_return_none(self, bad):
        assert implied_price_from_float(bad, 1e9) is None
        assert implied_price_from_float(1e9, bad) is None

    def test_implausible_fraction_rejected(self):
        assert implied_price_from_float(1e9, 1e9, non_affiliate_fraction=0.0) is None
        assert implied_price_from_float(1e9, 1e9, non_affiliate_fraction=1.5) is None


class TestCalibration:
    def test_recovers_a_known_affiliate_fraction(self):
        """Walmart: 391.7B float / (98.02 * 8.016B shares) = ~49.9% non-affiliate."""
        fraction = calibrate_non_affiliate_fraction(
            391.7e9, 8.016e9, 98.02,
            float_date=date(2025, 7, 31), price_date=date(2025, 7, 30),
        )
        assert fraction == pytest.approx(0.499, abs=0.01)

    def test_widely_held_company_is_near_one(self):
        fraction = calibrate_non_affiliate_fraction(
            3253.4e9, 15.056e9, 216.09,
            float_date=date(2025, 3, 28), price_date=date(2025, 3, 28),
        )
        assert fraction > 0.95

    def test_refuses_a_mismatched_date_pair(self):
        """The circular-calibration bug: a float and a price a year apart.

        The resulting "fraction" absorbs a year of price movement, and feeding it back through
        implied_price_from_float reproduces the input price exactly — so the pipeline looks
        calibrated while having learned nothing.
        """
        assert calibrate_non_affiliate_fraction(
            3253.4e9, 15.056e9, 250.12,
            float_date=date(2025, 3, 28), price_date=date(2026, 3, 15),
        ) is None

    def test_slightly_over_one_clamps_rather_than_rejects(self):
        fraction = calibrate_non_affiliate_fraction(
            100.5e9, 1e9, 100.0, float_date=date(2025, 1, 1), price_date=date(2025, 1, 2)
        )
        assert fraction == 1.0

    def test_calibrated_price_uses_the_newest_float(self):
        """Fraction from an old date-matched pair, applied to the latest float."""
        floats = pd.Series(
            {pd.Timestamp("2024-06-30"): 50e9, pd.Timestamp("2025-06-30"): 60e9}
        )
        prices = pd.Series({pd.Timestamp("2024-07-01"): 100.0})
        result = calibrated_price_from_float(floats, 1e9, prices)
        assert result is not None
        price, fraction, when = result
        assert fraction == pytest.approx(0.5, abs=0.01)
        assert price == pytest.approx(120.0, abs=1.0)  # 60e9 / (1e9 * 0.5)
        assert when == date(2025, 6, 30)

    def test_no_date_matched_pair_refuses(self):
        floats = pd.Series({pd.Timestamp("2020-06-30"): 50e9})
        prices = pd.Series({pd.Timestamp("2026-03-15"): 100.0})
        assert calibrated_price_from_float(floats, 1e9, prices) is None


class _FakeInsider:
    """Stands in for the provider so resolution logic is testable without a download."""

    def __init__(self, series: pd.Series | None) -> None:
        self._series = series

    def price_series(self, ticker, *, asof=None):
        return self._series

    def any_price(self, ticker, *, asof=None):
        if self._series is None or self._series.empty:
            return None
        return (float(self._series.iloc[-1]), self._series.index[-1].date())


class TestResolvePrice:
    def test_prefers_the_freshest_source(self):
        """Staleness beats source kind.

        A fixed priority order made Apple reject a 131-day-old insider price and fall back on a
        480-day-old public float — trading a good number for a much worse one.
        """
        insider = _FakeInsider(pd.Series({pd.Timestamp("2026-03-15"): 250.0}))
        floats = pd.Series({pd.Timestamp("2025-03-28"): 3253.4e9})
        found = resolve_price(
            "AAPL", asof=date(2026, 7, 24), insider=insider, public_float=3253.4e9,
            float_history=floats, shares_outstanding=15.056e9,
        )
        assert found is not None
        assert found["source"] == "sec_insider"
        assert found["date"] == date(2026, 3, 15)
        assert found["age_days"] == 131

    def test_reports_age(self):
        insider = _FakeInsider(pd.Series({pd.Timestamp("2026-06-01"): 100.0}))
        found = resolve_price(
            "X", asof=date(2026, 7, 24), insider=insider, public_float=None,
            float_history=None, shares_outstanding=1e9,
        )
        assert found["age_days"] == 53

    def test_refuses_everything_too_stale(self):
        insider = _FakeInsider(pd.Series({pd.Timestamp("2020-01-01"): 100.0}))
        assert resolve_price(
            "X", asof=date(2026, 7, 24), insider=insider, public_float=None,
            float_history=None, shares_outstanding=1e9, max_age_days=400,
        ) is None

    def test_falls_back_to_float_when_no_insider_data(self):
        insider = _FakeInsider(None)
        floats = pd.Series({pd.Timestamp("2026-06-30"): 50e9})
        found = resolve_price(
            "X", asof=date(2026, 7, 24), insider=insider, public_float=50e9,
            float_history=floats, shares_outstanding=1e9,
        )
        assert found is not None
        assert found["source"] == "public_float_lower_bound"

    def test_returns_none_with_nothing_available(self):
        assert resolve_price(
            "X", asof=date(2026, 7, 24), insider=_FakeInsider(None), public_float=None,
            float_history=None, shares_outstanding=1e9,
        ) is None
