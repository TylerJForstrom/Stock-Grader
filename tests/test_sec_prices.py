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
    check_price_share_basis,
    implied_price_from_float,
    resolve_price,
)


class TestTransactionCodes:
    def test_only_market_priced_codes_are_used(self):
        """Option exercises transact at the strike, not the market.

        Including code M would drag a price estimate toward strikes set years earlier; A (grant)
        and G (gift) are frequently recorded at zero.
        """
        assert {"S", "P", "F"} == MARKET_PRICED_CODES
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
            391.7e9,
            8.016e9,
            98.02,
            float_date=date(2025, 7, 31),
            price_date=date(2025, 7, 30),
        )
        assert fraction == pytest.approx(0.499, abs=0.01)

    def test_widely_held_company_is_near_one(self):
        fraction = calibrate_non_affiliate_fraction(
            3253.4e9,
            15.056e9,
            216.09,
            float_date=date(2025, 3, 28),
            price_date=date(2025, 3, 28),
        )
        assert fraction > 0.95

    def test_refuses_a_mismatched_date_pair(self):
        """The circular-calibration bug: a float and a price a year apart.

        The resulting "fraction" absorbs a year of price movement, and feeding it back through
        implied_price_from_float reproduces the input price exactly — so the pipeline looks
        calibrated while having learned nothing.
        """
        assert (
            calibrate_non_affiliate_fraction(
                3253.4e9,
                15.056e9,
                250.12,
                float_date=date(2025, 3, 28),
                price_date=date(2026, 3, 15),
            )
            is None
        )

    def test_slightly_over_one_clamps_rather_than_rejects(self):
        fraction = calibrate_non_affiliate_fraction(
            100.5e9, 1e9, 100.0, float_date=date(2025, 1, 1), price_date=date(2025, 1, 2)
        )
        assert fraction == 1.0

    def test_calibrated_price_uses_the_newest_float(self):
        """Fraction from an old date-matched pair, applied to the latest float."""
        floats = pd.Series({pd.Timestamp("2024-06-30"): 50e9, pd.Timestamp("2025-06-30"): 60e9})
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
            "AAPL",
            asof=date(2026, 7, 24),
            insider=insider,
            public_float=3253.4e9,
            float_history=floats,
            shares_outstanding=15.056e9,
        )
        assert found is not None
        assert found["source"] == "sec_insider"
        assert found["date"] == date(2026, 3, 15)
        assert found["age_days"] == 131

    def test_reports_age(self):
        insider = _FakeInsider(pd.Series({pd.Timestamp("2026-06-01"): 100.0}))
        found = resolve_price(
            "X",
            asof=date(2026, 7, 24),
            insider=insider,
            public_float=None,
            float_history=None,
            shares_outstanding=1e9,
        )
        assert found["age_days"] == 53

    def test_refuses_everything_too_stale(self):
        insider = _FakeInsider(pd.Series({pd.Timestamp("2020-01-01"): 100.0}))
        assert (
            resolve_price(
                "X",
                asof=date(2026, 7, 24),
                insider=insider,
                public_float=None,
                float_history=None,
                shares_outstanding=1e9,
                max_age_days=400,
            )
            is None
        )

    def test_falls_back_to_float_when_no_insider_data(self):
        insider = _FakeInsider(None)
        floats = pd.Series({pd.Timestamp("2026-06-30"): 50e9})
        found = resolve_price(
            "X",
            asof=date(2026, 7, 24),
            insider=insider,
            public_float=50e9,
            float_history=floats,
            shares_outstanding=1e9,
        )
        assert found is not None
        assert found["source"] == "public_float_lower_bound"
        assert found["valuation_eligible"] is False

    def test_returns_none_with_nothing_available(self):
        assert (
            resolve_price(
                "X",
                asof=date(2026, 7, 24),
                insider=_FakeInsider(None),
                public_float=None,
                float_history=None,
                shares_outstanding=1e9,
            )
            is None
        )


class TestPriceShareBasis:
    def test_detects_split_basis_contradiction(self):
        prices = pd.Series({pd.Timestamp("2020-06-30"): 20.0})
        floats = pd.Series({pd.Timestamp("2020-06-30"): 80e9})
        shares = pd.Series({pd.Timestamp("2020-07-15"): 1e9})

        result = check_price_share_basis(prices, floats, shares)

        assert result is not None
        assert result["status"] == "mismatch"
        assert result["public_to_total_share_ratio"] == pytest.approx(4.0)

    def test_accepts_compatible_price_and_share_units(self):
        prices = pd.Series({pd.Timestamp("2020-06-30"): 100.0})
        floats = pd.Series({pd.Timestamp("2020-06-30"): 80e9})
        shares = pd.Series({pd.Timestamp("2020-07-15"): 1e9})

        result = check_price_share_basis(prices, floats, shares)

        assert result is not None
        assert result["status"] == "not_contradicted"
        assert result["public_to_total_share_ratio"] == pytest.approx(0.8)

    def test_low_public_float_cannot_prove_split_basis_compatibility(self):
        prices = pd.Series({pd.Timestamp("2020-06-30"): 20.0})
        floats = pd.Series({pd.Timestamp("2020-06-30"): 20e9})
        shares = pd.Series({pd.Timestamp("2020-07-15"): 1e9})

        result = check_price_share_basis(prices, floats, shares)

        assert result is not None
        assert result["status"] == "not_contradicted"
        assert result["public_to_total_share_ratio"] == pytest.approx(1.0)


class TestBenchmark:
    """beta, capm_alpha and idiosyncratic_volatility declare needs_benchmark and read
    ``snapshot.benchmark`` — which nothing outside the test suite ever assigned, so all three were
    permanently MISSING in every configuration.
    """

    def test_benchmark_frame_has_the_columns_metrics_read(self):
        import pandas as pd

        from stock_grader.data.prices import BenchmarkProvider

        provider = BenchmarkProvider(cache_dir="/tmp/sg-test-bench")
        raw = pd.DataFrame(
            {"close": [100.0, 101.0]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"])
        )
        raw.to_csv(provider.cache_dir / "bench_SP500.csv")
        frame = provider.get("SP500")
        assert frame is not None
        assert "adj_close" in frame.columns and "close" in frame.columns

    def test_capm_metrics_fire_once_a_benchmark_exists(self):
        from datetime import date as _date

        import pandas as pd

        from stock_grader.data.synthetic import generate_panel
        from stock_grader.metrics import statistical  # noqa: F401
        from stock_grader.metrics.engine import evaluate_one
        from stock_grader.registry import METRICS
        from stock_grader.types import Coverage, SecuritySnapshot

        prices, benchmark, _ = generate_panel(["X"], n_days=700, seed=2, synthetic=True)
        # capm_alpha also declares needs_risk_free: a 0% rate is a different, flattering statistic.
        risk_free = pd.Series(0.05, index=prices["X"].index)
        snapshot = SecuritySnapshot(
            ticker="X",
            asof=_date(2026, 7, 24),
            prices=prices["X"],
            benchmark=benchmark,
            risk_free=risk_free,
        )
        for name in ("beta", "capm_alpha", "idiosyncratic_volatility"):
            assert evaluate_one(METRICS.get(name), snapshot).coverage is Coverage.OK, name

    def test_capm_metrics_are_missing_without_one(self):
        from datetime import date as _date

        from stock_grader.data.synthetic import generate_prices
        from stock_grader.metrics import statistical  # noqa: F401
        from stock_grader.metrics.engine import evaluate_one
        from stock_grader.registry import METRICS
        from stock_grader.types import Coverage, SecuritySnapshot

        snapshot = SecuritySnapshot(
            ticker="X",
            asof=_date(2026, 7, 24),
            prices=generate_prices("X", n_days=700, seed=2, synthetic=True),
        )
        result = evaluate_one(METRICS.get("beta"), snapshot)
        assert result.coverage is Coverage.MISSING
        assert "benchmark" in result.note


class TestStockAnalysisProvider:
    """The adjusted-close column is verified, not assumed.

    BRK.B has never paid a dividend, so its adjusted and raw closes must be identical (measured:
    100% of bars). AT&T pays a large one, so they must diverge (measured: 2016 close 42.38 against
    adjusted 17.80; ten-year price CAGR -5.5% versus adjusted +3.1% — the sign flips).
    """

    def test_payload_missing_the_adjusted_column_is_refused(self):
        """Guessing which column is the adjusted close would silently drop dividends."""

        from stock_grader.data.stockanalysis import StockAnalysisPriceProvider

        provider = StockAnalysisPriceProvider(cache_dir="/tmp/sg-sa-test")

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"t": "2026-01-02", "o": 1, "h": 2, "l": 1, "c": 2, "v": 10}]}

        provider._session.get = lambda *a, **k: _Response()  # type: ignore[assignment]
        assert provider._fetch("X", start=None, end=None) is None

    def test_unknown_symbol_is_refused_not_faked(self):
        from stock_grader.data.stockanalysis import StockAnalysisPriceProvider

        provider = StockAnalysisPriceProvider(cache_dir="/tmp/sg-sa-test2")

        class _Response:
            status_code = 404

            @staticmethod
            def json():
                return {}

        provider._session.get = lambda *a, **k: _Response()  # type: ignore[assignment]
        assert provider.get("NOTREAL") is None


class TestProfileWeightCoverage:
    """risk, momentum and liquidity computed correctly while every profile weighted them at zero.

    The pillars were added when no daily price series was reachable, so the profiles were written
    without them and never revisited once a source appeared.
    """

    def test_every_profile_weights_risk(self):
        from stock_grader.profiles import PROFILE_SPECS

        for name, spec in PROFILE_SPECS.items():
            assert spec["weights"].get("risk", 0.0) > 0.0, f"{name} ignores the risk pillar"

    def test_profile_weights_sum_to_one(self):
        from stock_grader.profiles import PROFILE_SPECS

        for name, spec in PROFILE_SPECS.items():
            assert abs(sum(spec["weights"].values()) - 1.0) < 1e-9, name

    def test_zero_weight_pillars_are_reported(self):
        """A computed pillar with no weight must say so rather than vanish."""

        from tests.test_pipeline import _universe

        from stock_grader.pipeline import GradeConfig, grade_universe

        config = GradeConfig(pillar_weights={"profitability": 1.0}, pillar_weighting="fixed")
        report = next(iter(grade_universe(_universe(6), config).values()))
        assert any("zero weight" in w for w in report.warnings)


class TestOfflineAndFailureHandling:
    def test_offline_mode_never_reaches_the_network(self):
        """--no-network gated the price providers but never reached the SEC client, so a
        cache-only run still fetched — and discarded a cache one second past its TTL rather than
        serving it, which is exactly backwards when the user asked not to use the network."""
        from stock_grader.data.sec import SECClient

        client = SECClient(cache_dir="/tmp/sg-offline-test", offline=True)

        def _poisoned(*args, **kwargs):
            raise AssertionError("network was used in offline mode")

        client._session.get = _poisoned  # type: ignore[method-assign]
        assert client.company_facts("0000000000") is None  # no cache, no network, no crash

    def test_circuit_breaker_stops_retrying(self):
        """A network outage cost 30 seconds of retry-sleeping per ticker — over 40 minutes for the
        default universe — and the command still exited 0."""
        import requests

        from stock_grader.data.sec import _CIRCUIT_BREAKER_THRESHOLD, SECClient

        client = SECClient(cache_dir="/tmp/sg-breaker-test")
        client._limiter.acquire = lambda: None  # type: ignore[method-assign]
        attempts = {"n": 0}

        def _fail(*args, **kwargs):
            attempts["n"] += 1
            raise requests.RequestException("down")

        client._session.get = _fail  # type: ignore[method-assign]
        import time as _time

        real_sleep, _time.sleep = _time.sleep, lambda *_: None
        try:
            for _ in range(_CIRCUIT_BREAKER_THRESHOLD + 3):
                client.get_json("https://example.invalid/x", "k")
        finally:
            _time.sleep = real_sleep
        assert client._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD
        # Once tripped, later calls must not attempt any further requests.
        before = attempts["n"]
        client.get_json("https://example.invalid/y", "k2")
        assert attempts["n"] == before


class TestPublicAPI:
    def test_importing_the_package_registers_the_whole_catalogue(self):
        """The registries fill by decorator side effect, so a consumer who imported only part of
        the catalogue got a silently truncated one and graded against a different metric set than
        the CLI, with no error and no way to tell."""
        import subprocess
        import sys

        snippet = (
            "import stock_grader as sg;"
            "print(len(sg.METRICS), len(sg.WEIGHTINGS), len(sg.NORMALIZERS), len(sg.AGGREGATORS))"
        )
        out = subprocess.run(
            [sys.executable, "-c", snippet], capture_output=True, text=True, check=True
        ).stdout.split()
        metrics, weightings = int(out[0]), int(out[1])
        assert metrics > 100, f"only {metrics} metrics registered on a plain import"
        assert weightings >= 23
