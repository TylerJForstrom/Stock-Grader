"""VaultDataSource contract tests against a fixture vault clone."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from stock_grader.data.vault import VaultDataSource, VaultError, VaultPriceProvider


def _gz_jsonl(rows) -> bytes:
    return gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))


def _manifest(directory: Path, names: list[str], *, corrupt: str | None = None) -> None:
    files = []
    for name in names:
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if corrupt == name:
            digest = "0" * 64
        files.append({"name": name, "sha256": digest, "bytes": (directory / name).stat().st_size})
    (directory / "manifest.json").write_text(
        json.dumps(
            {"schema_version": "1.0", "source_urls": [], "license_note": "private", "files": files}
        )
    )


def build_vault(root: Path, *, corrupt: str | None = None) -> Path:
    eod = root / "data" / "market_eod" / "2026-07"
    eod.mkdir(parents=True)
    day1 = [
        {
            "symbol": "AAPL",
            "open": 210.0,
            "high": 214.0,
            "low": 209.0,
            "close": 213.0,
            "volume": 1e6,
            "vwap": 212.0,
            "transactions": 1000,
        },
        {
            "symbol": "DEADCO",
            "open": 4.0,
            "high": 4.2,
            "low": 3.8,
            "close": 4.0,
            "volume": 5e4,
            "vwap": 4.0,
            "transactions": 42,
        },
        {
            "symbol": "BRK.B",
            "open": 470.0,
            "high": 472.0,
            "low": 468.0,
            "close": 471.0,
            "volume": 2e5,
            "vwap": 470.5,
            "transactions": 500,
        },
    ]
    day2 = [dict(day1[0], close=215.0), dict(day1[2], close=473.0)]  # DEADCO delisted
    (eod / "2026-07-27.jsonl.gz").write_bytes(_gz_jsonl(day1))
    (eod / "2026-07-28.jsonl.gz").write_bytes(_gz_jsonl(day2))
    _manifest(eod, ["2026-07-27.jsonl.gz", "2026-07-28.jsonl.gz"], corrupt=corrupt)

    borrow = root / "data" / "borrow" / "2026-07"
    borrow.mkdir(parents=True)
    snapshot = [
        {
            "symbol": "AAPL",
            "fee_rate": 0.25,
            "rebate_rate": 3.9,
            "available": 10_000_000,
            "available_capped": True,
            "currency": "USD",
        },
        {
            "symbol": "BRK B",
            "fee_rate": 0.30,
            "rebate_rate": 3.8,
            "available": 50_000,
            "available_capped": False,
            "currency": "USD",
        },
        {
            "symbol": "GME",
            "fee_rate": 13.75,
            "rebate_rate": -12.5,
            "available": 450_000,
            "available_capped": False,
            "currency": "USD",
        },
    ]
    (borrow / "usa_20260728T2318.jsonl.gz").write_bytes(_gz_jsonl(snapshot))
    _manifest(borrow, ["usa_20260728T2318.jsonl.gz"])

    dead = root / "data" / "delisted_prices" / "2023"
    dead.mkdir(parents=True)
    history = {
        "data": {
            "data": [
                {"t": 1672531200, "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5, "a": 10.5, "v": 1000},
                {"t": 1672617600, "o": 10.5, "h": 10.8, "l": 10.0, "c": 10.2, "a": 10.2, "v": 900},
            ]
        }
    }
    (dead / "SIVB.json.gz").write_bytes(gzip.compress(json.dumps(history).encode()))
    (dead / "cohort_index.json").write_text("{}")
    _manifest(dead, ["SIVB.json.gz", "cohort_index.json"])
    return root


def test_requires_a_vault_shaped_root(tmp_path):
    with pytest.raises(VaultError, match="does not look like"):
        VaultDataSource(tmp_path)


def test_eod_day_and_series_including_delisted(tmp_path):
    vault = VaultDataSource(build_vault(tmp_path))
    day = vault.market_eod_day(dt.date(2026, 7, 27))
    assert set(day["symbol"]) == {"AAPL", "DEADCO", "BRK.B"}
    dead = vault.market_eod_series("DEADCO")
    assert len(dead) == 1  # present until delisting, then honestly absent
    apple = vault.market_eod_series("AAPL")
    assert list(apple["close"]) == [213.0, 215.0]


def test_ticker_spelling_bridge_polygon_dots(tmp_path):
    vault = VaultDataSource(build_vault(tmp_path))
    series = vault.market_eod_series("BRK-B")  # SEC dash form finds Polygon dot form
    assert series is not None and list(series["close"]) == [471.0, 473.0]


def test_borrow_fee_bridges_ib_space_notation(tmp_path):
    vault = VaultDataSource(build_vault(tmp_path))
    row = vault.borrow_fee("BRK.B")
    assert row is not None and row["fee_rate"] == pytest.approx(0.30)
    gme = vault.borrow_fee("GME")
    assert gme["fee_rate"] == pytest.approx(13.75)  # hard-to-borrow red flag material


def test_delisted_history_reads_cohort_archive(tmp_path):
    vault = VaultDataSource(build_vault(tmp_path))
    history = vault.delisted_history("SIVB")
    assert history is not None and len(history) == 2
    assert history["c"].iloc[-1] == pytest.approx(10.2)


def test_hash_mismatch_refused(tmp_path):
    vault = VaultDataSource(build_vault(tmp_path, corrupt="2026-07-27.jsonl.gz"))
    with pytest.raises(VaultError, match="sha256 mismatch"):
        vault.market_eod_day(dt.date(2026, 7, 27))


def test_price_provider_adapter_shapes_frames(tmp_path):
    provider = VaultPriceProvider(VaultDataSource(build_vault(tmp_path)))
    frame = provider._fetch("AAPL")
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame["close"].iloc[-1] == pytest.approx(215.0)


def test_market_eod_panel_reads_each_day_once_and_series_matches_fixture(
    tmp_path,
    monkeypatch,
):
    from stock_grader.data import vault as vault_module

    root = build_vault(tmp_path / "vault")
    vault = VaultDataSource(root)
    cache = tmp_path / "cache"
    monkeypatch.setattr(vault_module, "default_cache_dir", lambda *_parts: cache)
    calls: list[tuple[str, str]] = []
    original = vault._read_verified

    def counted(dataset_dir: str, name: str) -> bytes:
        calls.append((dataset_dir, name))
        return original(dataset_dir, name)

    monkeypatch.setattr(vault, "_read_verified", counted)
    panel = vault.market_eod_panel()
    assert list(panel.columns) == [
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "transactions",
    ]
    day_reads = [name for dataset, name in calls if dataset.startswith("data/market_eod/")]
    assert day_reads == ["2026-07-27.jsonl.gz", "2026-07-28.jsonl.gz"]

    series = vault.market_eod_series("BRK-B")
    assert series is not None
    assert list(series["close"]) == [471.0, 473.0]
    expected = panel[panel["symbol"].eq("BRK.B")].set_index("date").sort_index()
    pd.testing.assert_frame_equal(series, expected)
    assert [name for dataset, name in calls if dataset.startswith("data/market_eod/")] == day_reads


def test_vault_price_provider_is_a_real_chained_provider_and_holds_panel(
    tmp_path,
    monkeypatch,
):
    root = build_vault(tmp_path / "vault")
    vault = VaultDataSource(root)
    builds = 0
    original = vault.market_eod_panel

    def counted_panel(*args, **kwargs):
        nonlocal builds
        builds += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(vault, "market_eod_panel", counted_panel)
    provider = VaultPriceProvider(vault, cache_dir=tmp_path / "cache")
    first = provider.get("AAPL", end=dt.date(2026, 7, 28))
    second = provider.get("BRK-B", end=dt.date(2026, 7, 28))
    assert first is not None and first["close"].iloc[-1] == pytest.approx(215.0)
    assert second is not None and second["close"].iloc[-1] == pytest.approx(473.0)
    assert builds == 1


@pytest.mark.parametrize("bad_value", ["not-a-number", True])
def test_market_eod_panel_refuses_unparseable_numeric_feed_values(tmp_path, bad_value):
    root = build_vault(tmp_path / "vault")
    eod = root / "data" / "market_eod" / "2026-07"
    path = eod / "2026-07-27.jsonl.gz"
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).decode().splitlines()]
    rows[0]["close"] = bad_value
    path.write_bytes(_gz_jsonl(rows))
    _manifest(eod, ["2026-07-27.jsonl.gz", "2026-07-28.jsonl.gz"])
    with pytest.raises(VaultError, match="unparseable numeric"):
        VaultDataSource(root).market_eod_panel(cache_dir=tmp_path / "cache")

    provider = VaultPriceProvider(VaultDataSource(root), cache_dir=tmp_path / "provider-cache")
    with pytest.raises(VaultError, match="unparseable numeric"):
        provider.get("AAPL", end=dt.date(2026, 7, 28))


def test_dividend_archive_reads_verified_and_range_filtered(tmp_path):
    root = tmp_path / "vault"
    (root / "data").mkdir(parents=True)
    month = root / "data" / "dividends" / "2026-08"
    month.mkdir(parents=True)
    records = [
        {
            "ticker": "AAPL",
            "ex_dividend_date": "2026-08-08",
            "cash_amount": 0.26,
            "currency": "USD",
            "dividend_type": "CD",
        }
    ]
    (month / "2026-08.jsonl.gz").write_bytes(_gz_jsonl(records))
    _manifest(month, ["2026-08.jsonl.gz"])

    vault = VaultDataSource(root)
    assert vault.dividend_months() == ["2026-08"]
    frame = vault.dividends(dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    assert frame.iloc[0]["cash_amount"] == pytest.approx(0.26)
    assert frame.iloc[0]["ex_dividend_date"] == dt.date(2026, 8, 8)
    assert vault.dividends(dt.date(2026, 9, 1), dt.date(2026, 9, 30)).empty

    # Same manifest discipline as every other vault dataset: tamper -> refuse.
    _manifest(month, ["2026-08.jsonl.gz"], corrupt="2026-08.jsonl.gz")
    with pytest.raises(VaultError, match="sha256"):
        VaultDataSource(root).dividends()

    # A clone that predates the collector has no months and no opinions.
    bare = tmp_path / "bare"
    (bare / "data").mkdir(parents=True)
    assert VaultDataSource(bare).dividend_months() == []
