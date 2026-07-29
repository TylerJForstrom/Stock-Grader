"""Regression tests for the §1 residual fixes: shared ticker normalization,
stale-if-error serving, SEC-client routing for bulk files, and versioned
derived caches."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import pytest
import requests

from stock_grader.data.sec import SECClient
from stock_grader.data.sec_prices import (
    _CACHE_SCHEMA_VERSION,
    SECInsiderPriceProvider,
    _quarter_cache_path,
)
from stock_grader.data.symbols import ticker_variants


class TestTickerVariants:
    def test_dot_and_dash_forms_both_tried(self):
        assert ticker_variants("BRK.B") == ("BRK.B", "BRK-B")
        assert ticker_variants("brk-b") == ("BRK-B", "BRK.B")

    def test_plain_ticker_single_variant(self):
        assert ticker_variants(" aapl ") == ("AAPL",)


class TestStaleIfError:
    def _client_with_stale_cache(self, tmp_path: Path, payload: dict) -> SECClient:
        client = SECClient(cache_dir=tmp_path, contact="test@example.com")
        path = client._cache_path("companyfacts_test")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        # Age the file beyond any TTL so the fresh-cache branch cannot serve it.
        old = time.time() - 10 * 24 * 3600
        os.utime(path, (old, old))
        return client

    def test_stale_served_after_network_failure(self, tmp_path, monkeypatch):
        client = self._client_with_stale_cache(tmp_path, {"facts": "stale-but-real"})

        def explode(*args, **kwargs):
            raise requests.ConnectionError("network down")

        monkeypatch.setattr(client._session, "get", explode)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        payload = client.get_json("https://data.sec.gov/x", "companyfacts_test")
        assert payload == {"facts": "stale-but-real"}

    def test_stale_served_when_circuit_open(self, tmp_path):
        client = self._client_with_stale_cache(tmp_path, {"facts": "from-cache"})
        client._consecutive_failures = 99
        payload = client.get_json("https://data.sec.gov/x", "companyfacts_test")
        assert payload == {"facts": "from-cache"}

    def test_no_cache_still_returns_none(self, tmp_path, monkeypatch):
        client = SECClient(cache_dir=tmp_path, contact="test@example.com")

        def explode(*args, **kwargs):
            raise requests.ConnectionError("network down")

        monkeypatch.setattr(client._session, "get", explode)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        assert client.get_json("https://data.sec.gov/x", "missing_key") is None


class _StubClient:
    """Stands in for SECClient in the insider provider."""

    def __init__(self, content: bytes | None):
        self.content = content
        self.calls: list[str] = []

    def get_bytes(self, url: str) -> bytes | None:
        self.calls.append(url)
        return self.content


class TestInsiderRouting:
    def test_download_goes_through_injected_client(self, tmp_path):
        stub = _StubClient(content=None)
        provider = SECInsiderPriceProvider(cache_dir=tmp_path, client=stub)
        assert provider._load_quarter("2026q1") is None
        assert len(stub.calls) == 1
        assert "2026q1" in stub.calls[0]
        # A client-level failure must count toward the provider's breaker.
        assert provider._network_failures == 1

    def test_default_client_is_a_sec_client(self, tmp_path):
        provider = SECInsiderPriceProvider(cache_dir=tmp_path)
        assert hasattr(provider._client, "get_bytes")
        assert type(provider._client).__name__ == "SECClient"


class TestVersionedCache:
    def test_quarter_cache_filename_carries_schema_version(self, tmp_path):
        path = _quarter_cache_path(tmp_path, "2026q1")
        assert path.name == f"2026q1_v{_CACHE_SCHEMA_VERSION}.parquet"

    def test_old_unversioned_cache_is_ignored(self, tmp_path):
        # A pre-versioning cache file must not be picked up.
        legacy = tmp_path / "2026q1.parquet"
        pd.DataFrame({"ticker": ["AAPL"]}).to_parquet(legacy)
        stub = _StubClient(content=None)
        provider = SECInsiderPriceProvider(cache_dir=tmp_path, client=stub)
        provider._load_quarter("2026q1")
        assert stub.calls, "provider should refetch instead of reading the legacy cache"

    def test_invalid_quarter_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _quarter_cache_path(tmp_path, "../../evil")
