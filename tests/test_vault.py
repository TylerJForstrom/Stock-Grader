"""VaultDataSource contract tests against a fixture vault clone."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

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
        json.dumps({"schema_version": "1.0", "source_urls": [], "license_note": "private",
                    "files": files})
    )


def build_vault(root: Path, *, corrupt: str | None = None) -> Path:
    eod = root / "data" / "market_eod" / "2026-07"
    eod.mkdir(parents=True)
    day1 = [
        {"symbol": "AAPL", "open": 210.0, "high": 214.0, "low": 209.0, "close": 213.0,
         "volume": 1e6, "vwap": 212.0, "transactions": 1000},
        {"symbol": "DEADCO", "open": 4.0, "high": 4.2, "low": 3.8, "close": 4.0,
         "volume": 5e4, "vwap": 4.0, "transactions": 42},
        {"symbol": "BRK.B", "open": 470.0, "high": 472.0, "low": 468.0, "close": 471.0,
         "volume": 2e5, "vwap": 470.5, "transactions": 500},
    ]
    day2 = [dict(day1[0], close=215.0), dict(day1[2], close=473.0)]  # DEADCO delisted
    (eod / "2026-07-27.jsonl.gz").write_bytes(_gz_jsonl(day1))
    (eod / "2026-07-28.jsonl.gz").write_bytes(_gz_jsonl(day2))
    _manifest(eod, ["2026-07-27.jsonl.gz", "2026-07-28.jsonl.gz"], corrupt=corrupt)

    borrow = root / "data" / "borrow" / "2026-07"
    borrow.mkdir(parents=True)
    snapshot = [
        {"symbol": "AAPL", "fee_rate": 0.25, "rebate_rate": 3.9,
         "available": 10_000_000, "available_capped": True, "currency": "USD"},
        {"symbol": "BRK B", "fee_rate": 0.30, "rebate_rate": 3.8,
         "available": 50_000, "available_capped": False, "currency": "USD"},
        {"symbol": "GME", "fee_rate": 13.75, "rebate_rate": -12.5,
         "available": 450_000, "available_capped": False, "currency": "USD"},
    ]
    (borrow / "usa_20260728T2318.jsonl.gz").write_bytes(_gz_jsonl(snapshot))
    _manifest(borrow, ["usa_20260728T2318.jsonl.gz"])

    dead = root / "data" / "delisted_prices" / "2023"
    dead.mkdir(parents=True)
    history = {"data": {"data": [
        {"t": 1672531200, "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5, "a": 10.5, "v": 1000},
        {"t": 1672617600, "o": 10.5, "h": 10.8, "l": 10.0, "c": 10.2, "a": 10.2, "v": 900},
    ]}}
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
