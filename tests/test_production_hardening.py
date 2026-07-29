"""Regression tests for production-hardening behavior at external data boundaries."""

from __future__ import annotations

import os
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import requests

from stock_grader import weighting
from stock_grader.data import cache as cache_paths
from stock_grader.data.cache import default_cache_dir
from stock_grader.data.prices import (
    AdjustedPriceStatus,
    BenchmarkProvider,
    ChainedPriceProvider,
    CSVPriceProvider,
    RiskFreeProvider,
    StooqPriceProvider,
    TiingoPriceProvider,
    YahooPriceProvider,
    _safe_cache_path,
    _utc_epoch,
    validate_price_frame,
)
from stock_grader.data.sec import SECClient
from stock_grader.data.sec_prices import SECInsiderPriceProvider
from stock_grader.data.stockanalysis import StockAnalysisPriceProvider


class TestPlatformCachePaths:
    def test_windows_defaults_use_local_app_data_for_every_provider(self, tmp_path, monkeypatch):
        local = tmp_path / "LocalAppData"
        monkeypatch.setattr(cache_paths, "_is_windows", lambda: True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "wrong-platform"))

        root = local / "stock-grader"
        assert default_cache_dir() == root
        assert SECClient(offline=True).cache_dir == root
        assert RiskFreeProvider().cache_dir == root.resolve()
        assert BenchmarkProvider().cache_dir == root.resolve()
        assert SECInsiderPriceProvider().cache_dir == (root / "insider").resolve()
        assert StockAnalysisPriceProvider().cache_dir == root / "sa"

    def test_unix_defaults_honor_xdg_cache_home(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        monkeypatch.setattr(cache_paths, "_is_windows", lambda: False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "wrong-platform"))

        assert default_cache_dir("prices") == xdg / "stock-grader" / "prices"

    def test_windows_backslashes_cannot_escape_sec_or_stockanalysis_cache(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cache_paths, "_is_windows", lambda: True)
        sec = SECClient(cache_dir=tmp_path, offline=True)
        stockanalysis = StockAnalysisPriceProvider(cache_dir=tmp_path)

        sec_path = sec._cache_path(r"..\outside")
        stockanalysis_path = stockanalysis._cache_path(r"..\outside")
        assert sec_path.parent == tmp_path.resolve()
        assert stockanalysis_path.parent == tmp_path.resolve()
        assert sec_path.name.startswith("id-")
        assert stockanalysis_path.name.startswith("id-")

    def test_safe_cache_identifiers_keep_stable_filenames(self, tmp_path):
        sec = SECClient(cache_dir=tmp_path, offline=True)
        stockanalysis = StockAnalysisPriceProvider(cache_dir=tmp_path, range_="10y")

        assert sec._cache_path("facts_0000320193").name == "facts_0000320193.json"
        assert stockanalysis._cache_path("BRK.B").name == "BRK.B_10y.parquet"


class TestDensePriceValidation:
    def test_quality_diagnostics_are_explicit_and_attached(self):
        raw = pd.DataFrame(
            {
                "open": [10.0, 11.0, 12.0, 8.0],
                "high": [11.0, 12.0, 11.0, 9.0],
                "low": [9.0, 10.0, 10.0, 7.0],
                "close": [10.5, 11.5, 12.5, -1.0],
                "volume": [100.0, 100.0, -1.0, 100.0],
            },
            index=["2026-01-02", "not-a-date", "2026-01-02", "2026-02-02"],
        )

        frame, diagnostics = validate_price_frame(
            raw,
            asof=date(2026, 2, 10),
            stale_after_days=5,
        )

        assert list(frame.columns) == ["open", "high", "low", "close", "adj_close", "volume"]
        assert diagnostics.adjusted_status is AdjustedPriceStatus.DERIVED_FROM_CLOSE
        assert diagnostics.invalid_date_rows == 1
        assert diagnostics.duplicate_date_rows == 1
        assert diagnostics.nonpositive_price_rows == 1
        assert diagnostics.negative_volume_rows == 1
        assert diagnostics.inconsistent_ohlc_rows == 1
        assert diagnostics.stale
        assert frame.attrs["adjusted_status"] == "derived_from_close"
        assert frame.attrs["price_quality"]["invalid_date_rows"] == 1

    def test_native_adjusted_prices_are_not_overwritten(self):
        raw = pd.DataFrame(
            {"close": [100.0], "adj_close": [91.0]},
            index=pd.to_datetime(["2026-01-02"]),
        )
        frame, diagnostics = validate_price_frame(raw, asof=date(2026, 1, 2))
        assert diagnostics.adjusted_status is AdjustedPriceStatus.NATIVE
        assert frame.iloc[0]["adj_close"] == pytest.approx(91.0)

    def test_sparse_business_day_coverage_is_reported(self):
        raw = pd.DataFrame(
            {"close": [100.0, 101.0]},
            index=pd.to_datetime(["2026-01-02", "2026-02-02"]),
        )
        _, diagnostics = validate_price_frame(raw, asof=date(2026, 2, 2))
        assert diagnostics.coverage_ratio is not None
        assert diagnostics.coverage_ratio < 0.1
        assert any("coverage is sparse" in warning for warning in diagnostics.warnings)

    def test_future_observations_are_refused_at_the_analysis_date(self):
        raw = pd.DataFrame(
            {"close": [100.0, 9_999.0]},
            index=pd.to_datetime(["2026-01-02", "2027-01-02"]),
        )
        frame, diagnostics = validate_price_frame(raw, asof=date(2026, 1, 2))
        assert diagnostics.future_date_rows == 1
        assert frame["close"].tolist() == [100.0]

    def test_provider_refuses_a_stale_dense_frame(self, tmp_path):
        (tmp_path / "DELISTED.csv").write_text(
            "date,close\n2020-01-02,10.0\n",
            encoding="utf-8",
        )
        provider = CSVPriceProvider(tmp_path)

        assert provider.get("DELISTED", end=date(2026, 1, 2)) is None
        assert provider.last_diagnostics is not None
        assert provider.last_diagnostics.stale

    def test_chain_preserves_stale_rejection_diagnostics(self, tmp_path):
        (tmp_path / "DELISTED.csv").write_text(
            "date,close\n2020-01-02,10.0\n",
            encoding="utf-8",
        )
        chain = ChainedPriceProvider([CSVPriceProvider(tmp_path)])

        assert chain.get("DELISTED", end=date(2026, 1, 2)) is None
        assert chain.last_diagnostics is not None
        assert chain.last_rejections[0]["provider"] == "csv"
        quality = chain.last_rejections[0]["price_quality"]
        assert isinstance(quality, dict)
        assert quality["stale"] is True


class TestPathAndCacheSafety:
    def test_csv_ticker_cannot_traverse_outside_configured_directory(self, tmp_path):
        price_dir = tmp_path / "prices"
        price_dir.mkdir()
        outside = tmp_path / "secret.csv"
        outside.write_text("date,close\n2026-01-02,100\n", encoding="utf-8")

        provider = CSVPriceProvider(price_dir)
        assert provider.get("../secret") is None

    def test_untrusted_cache_identifier_is_hashed_inside_root(self, tmp_path):
        cache = _safe_cache_path(tmp_path, "fred", "../../outside", ".csv")
        assert cache.parent == tmp_path.resolve()
        assert ".." not in cache.name
        assert cache.name.startswith("fred_id-")

    def test_risk_free_uses_stale_cache_when_refresh_fails(self, tmp_path, monkeypatch):
        cache = tmp_path / "fred_DTB3.csv"
        pd.Series(
            [5.0],
            index=pd.to_datetime(["2026-01-02"]),
            name="value",
        ).to_csv(cache)
        os.utime(cache, (0, 0))

        class Response:
            status_code = 503

        monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())
        result = RiskFreeProvider(cache_dir=tmp_path, ttl_hours=1).get()
        assert result is not None
        assert result.iloc[0] == pytest.approx(0.05)
        assert result.attrs["cache_status"] == "stale_fallback"

    def test_benchmark_uses_stale_cache_when_refresh_fails(self, tmp_path, monkeypatch):
        cache = tmp_path / "bench_SP500.csv"
        pd.Series(
            [5000.0],
            index=pd.to_datetime(["2026-01-02"]),
            name="close",
        ).to_csv(cache)
        os.utime(cache, (0, 0))

        class Response:
            status_code = 503

        monkeypatch.setattr(requests, "get", lambda *args, **kwargs: Response())
        result = BenchmarkProvider(cache_dir=tmp_path, ttl_hours=1).get()
        assert result is not None
        assert result.iloc[0]["close"] == pytest.approx(5000.0)
        assert result.attrs["cache_status"] == "stale_fallback"


class TestNetworkRequestHardening:
    def test_yahoo_end_only_request_is_anchored_to_historical_end(self):
        provider = YahooPriceProvider()
        captured: dict[str, object] = {}

        class Response:
            status_code = 404

        def fake_get(url, *, params, timeout):
            captured.update(params)
            return Response()

        provider._session.get = fake_get  # type: ignore[method-assign]
        requested_end = date(2020, 3, 15)
        assert provider._fetch("AAPL", start=None, end=requested_end) is None
        assert captured["period1"] == _utc_epoch(requested_end - timedelta(days=3653))
        assert captured["period2"] == _utc_epoch(requested_end + timedelta(days=1))
        assert "range" not in captured
        assert captured["events"] == "div,split"

    def test_yahoo_refuses_unreliable_daily_ranges_beyond_ten_years(self):
        provider = YahooPriceProvider()
        provider._session.get = lambda *args, **kwargs: pytest.fail(  # type: ignore[method-assign]
            "an unsupported range must be refused before a network request"
        )

        assert (
            provider._fetch(
                "AAPL",
                start=date(2000, 1, 1),
                end=date(2026, 1, 1),
            )
            is None
        )

    def test_yahoo_preserves_explicit_split_events_for_share_basis_reconciliation(self):
        provider = YahooPriceProvider()
        stamp = _utc_epoch(date(2026, 1, 2))

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "chart": {
                        "result": [
                            {
                                "timestamp": [stamp],
                                "indicators": {
                                    "quote": [
                                        {
                                            "open": [100.0],
                                            "high": [101.0],
                                            "low": [99.0],
                                            "close": [100.0],
                                            "volume": [1_000.0],
                                        }
                                    ],
                                    "adjclose": [{"adjclose": [100.0]}],
                                },
                                "events": {
                                    "splits": {
                                        str(stamp): {
                                            "date": stamp,
                                            "numerator": 4.0,
                                            "denominator": 1.0,
                                            "splitRatio": "4:1",
                                        }
                                    }
                                },
                            }
                        ]
                    }
                }

        provider._session.get = lambda *args, **kwargs: Response()  # type: ignore[method-assign]

        frame = provider._fetch("AAPL", start=None, end=None)

        assert frame is not None
        assert frame.attrs["split_events"] == [
            {"date": "2026-01-02", "factor": 4.0, "ratio": "4:1"}
        ]

    def test_yahoo_circuit_breaker_stops_repeated_rate_limit_calls(self):
        provider = YahooPriceProvider(failure_threshold=2, cooldown_seconds=3600)
        calls = 0

        class Response:
            status_code = 429

        def fake_get(*args, **kwargs):
            nonlocal calls
            calls += 1
            return Response()

        provider._session.get = fake_get  # type: ignore[method-assign]
        for ticker in ("A", "B", "C"):
            assert provider._fetch(ticker, start=None, end=None) is None
        assert calls == 2

    def test_stooq_bot_page_opens_circuit_breaker(self):
        provider = StooqPriceProvider(failure_threshold=2, cooldown_seconds=3600)
        calls = 0

        class Response:
            status_code = 200
            text = "<!doctype html><p>requires JavaScript</p>"

        def fake_get(*args, **kwargs):
            nonlocal calls
            calls += 1
            return Response()

        provider._session.get = fake_get  # type: ignore[method-assign]
        for ticker in ("A", "B", "C"):
            assert provider._fetch(ticker, start=None, end=None) is None
        assert calls == 2

    def test_tiingo_auth_failure_opens_circuit_breaker(self):
        provider = TiingoPriceProvider(
            api_key="invalid",
            failure_threshold=2,
            cooldown_seconds=3600,
        )
        calls = 0

        class Response:
            status_code = 401

        def fake_get(*args, **kwargs):
            nonlocal calls
            calls += 1
            return Response()

        provider._session.get = fake_get  # type: ignore[method-assign]
        for ticker in ("A", "B", "C"):
            assert provider._fetch(ticker, start=None, end=None) is None
        assert calls == 2


class TestSECInsiderCacheScoping:
    def test_in_memory_tables_are_keyed_by_asof_quarter_set(self, tmp_path, monkeypatch):
        provider = SECInsiderPriceProvider(cache_dir=tmp_path, quarters=1)
        loaded: list[str] = []

        def fake_load(quarter: str, *, refresh: bool = False):
            loaded.append(quarter)
            return pd.DataFrame(
                {
                    "ticker": ["AAPL"],
                    "date": pd.to_datetime(["2025-12-15"]),
                    "price": [200.0],
                }
            )

        monkeypatch.setattr(provider, "_load_quarter", fake_load)
        provider.load(asof=date(2026, 2, 1))
        provider.load(asof=date(2026, 8, 1))
        provider.load(asof=date(2026, 2, 20))
        assert loaded == ["2025q4", "2026q2"]

        provider.load(asof=date(2026, 2, 20), refresh=True)
        assert loaded[-1] == "2025q4"

    def test_historical_coverage_excludes_rows_after_asof(self, tmp_path, monkeypatch):
        provider = SECInsiderPriceProvider(cache_dir=tmp_path, quarters=1)
        table = pd.DataFrame(
            {
                "ticker": ["KNOWN", "FUTURE"],
                "date": pd.to_datetime(["2025-12-15", "2026-01-15"]),
                "price": [100.0, 200.0],
            }
        )
        monkeypatch.setattr(provider, "load", lambda *, asof=None, refresh=False: table)

        assert provider.coverage(asof=date(2025, 12, 31)) == 1

    def test_invalid_quarter_cannot_escape_cache(self, tmp_path, monkeypatch):
        provider = SECInsiderPriceProvider(cache_dir=tmp_path)
        monkeypatch.setattr(
            requests,
            "get",
            lambda *args, **kwargs: pytest.fail("network should not be reached"),
        )
        assert provider._load_quarter("../../outside") is None

    def test_incomplete_refresh_retains_last_complete_table(self, tmp_path, monkeypatch):
        provider = SECInsiderPriceProvider(cache_dir=tmp_path, quarters=1)
        original = pd.DataFrame(
            {
                "ticker": ["AAPL"],
                "date": pd.to_datetime(["2025-12-15"]),
                "price": [200.0],
            }
        )
        monkeypatch.setattr(provider, "_load_quarter", lambda *args, **kwargs: original)
        first = provider.load(asof=date(2026, 2, 1))

        monkeypatch.setattr(provider, "_load_quarter", lambda *args, **kwargs: None)
        refreshed = provider.load(asof=date(2026, 2, 1), refresh=True)
        pd.testing.assert_frame_equal(refreshed, first)

    def test_sec_circuit_breaker_stops_repeated_rate_limit_calls(self, tmp_path):
        # Downloads route through the shared SEC client now; a client that has
        # exhausted its own retries reports None, and the provider's breaker
        # must stop asking after failure_threshold quarters.
        class ExhaustedClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_bytes(self, url: str) -> bytes | None:
                self.calls += 1
                return None

        stub = ExhaustedClient()
        provider = SECInsiderPriceProvider(
            cache_dir=tmp_path,
            failure_threshold=2,
            cooldown_seconds=3600,
            client=stub,
        )
        for quarter in ("2025q1", "2025q2", "2025q3"):
            assert provider._load_quarter(quarter, refresh=True) is None
        assert stub.calls == 2


def test_shapley_checks_factorial_budget_before_enumerating(monkeypatch):
    rng = np.random.default_rng(42)
    columns = [f"metric_{index}" for index in range(10)]
    panel = pd.DataFrame(rng.normal(size=(40, 10)), columns=columns)
    forward_returns = panel["metric_0"] + rng.normal(scale=0.1, size=len(panel))
    context = weighting.WeightingContext(
        forward_returns=forward_returns,
        config={
            "shapley_permutations": 7,
            "shapley_max_exact_permutations": 5000,
        },
        seed=7,
    )

    def exact_enumeration_would_be_a_bug(*args, **kwargs):
        raise AssertionError("exact permutation enumeration was attempted")

    monkeypatch.setattr(weighting, "permutations", exact_enumeration_would_be_a_bug)
    result = weighting.shapley_weights(panel, context)

    assert result.sum() == pytest.approx(1.0)
    assert context.config["shapley_sampled"] is True
    assert context.config["shapley_orderings_evaluated"] == 7
