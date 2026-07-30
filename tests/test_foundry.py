"""FoundryDataSource contract tests: manifest enforcement, hash verification,
and dataset reads against a fixture foundry tree."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from stock_grader.data.foundry import FoundryDataSource, FoundryError


def build_foundry(root: Path, *, schema="1.0", corrupt: str | None = None) -> Path:
    symbols_dir = root / "data" / "symbols" / "current"
    actions_dir = root / "data" / "corporate_actions"
    symbols_dir.mkdir(parents=True)
    actions_dir.mkdir(parents=True)

    exchange_rows = [
        {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"},
        {"cik": 1067983, "ticker": "BRK-B", "title": "Berkshire", "exchange": "NYSE"},
        {"cik": 999999, "ticker": "SCAMCO", "title": "Pink Sheet Co", "exchange": ""},
    ]
    symbols_file = symbols_dir / "sec_company_tickers_exchange.jsonl"
    symbols_file.write_bytes(
        "".join(json.dumps(r) + "\n" for r in exchange_rows).encode("utf-8")
    )

    dividends = pd.DataFrame(
        [
            {"ticker": "AAPL", "period_start": "2025-10-01", "period_end": "2025-12-27",
             "span_type": "quarterly", "dps_current_basis": 0.25, "derived": False,
             "approximate": False, "flags": ""},
            {"ticker": "AAPL", "period_start": "2025-12-28", "period_end": "2026-03-28",
             "span_type": "quarterly", "dps_current_basis": 0.25, "derived": False,
             "approximate": False, "flags": ""},
            {"ticker": "AAPL", "period_start": "2019-01-01", "period_end": "2019-03-30",
             "span_type": "quarterly", "dps_current_basis": 0.19, "derived": False,
             "approximate": False, "flags": ""},  # outside any trailing window
            {"ticker": "AAPL", "period_start": "2025-01-01", "period_end": "2025-12-27",
             "span_type": "annual", "dps_current_basis": 1.0, "derived": False,
             "approximate": False, "flags": ""},  # annual rows must not double-count
        ]
    )
    dividends_file = actions_dir / "dividends.parquet"
    dividends.to_parquet(dividends_file, index=False)

    splits_file = actions_dir / "splits.jsonl"
    splits_file.write_bytes(
        (json.dumps({"ticker": "AAPL", "effective_date": "2020-08-28", "ratio": 4.0,
                     "confidence": "high", "filed": "2020-10-30"}) + "\n").encode("utf-8")
    )

    events_dir = root / "data" / "symbols" / "events"
    events_dir.mkdir(parents=True)
    events = [
        # DEADCO delisted on 07-20 (removal event); NEWCO listed on 07-25
        {"date": "2026-07-20", "source": "sec_company_tickers_exchange", "event": "removed",
         "record": {"cik": 555, "ticker": "DEADCO", "title": "Dead Co", "exchange": "NYSE"}},
        {"date": "2026-07-25", "source": "sec_company_tickers_exchange", "event": "added",
         "record": {"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"}},
    ]
    events_file = events_dir / "events.jsonl"
    events_file.write_bytes("".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))

    for directory, names in (
        (symbols_dir, [symbols_file.name]),
        (actions_dir, [dividends_file.name, splits_file.name]),
        (events_dir, [events_file.name]),
    ):
        files = []
        for name in names:
            blob = (directory / name).read_bytes()
            digest = hashlib.sha256(blob).hexdigest()
            if corrupt == name:
                digest = "0" * 64
            files.append({"name": name, "sha256": digest, "bytes": len(blob)})
        (directory / "manifest.json").write_text(
            json.dumps({"schema_version": schema, "source_urls": [], "license_note": "test",
                        "files": files})
        )
    return root


def test_universe_filters_unlisted_venues(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    tickers = source.universe_tickers()
    assert tickers == ["AAPL", "BRK-B"]  # SCAMCO (no listed exchange) excluded
    everything = source.universe_tickers(listed_only=False)
    assert "SCAMCO" in everything


def test_unknown_schema_version_refused(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path, schema="9.9"))
    with pytest.raises(FoundryError, match="schema_version"):
        source.universe_tickers()


def test_hash_mismatch_refused(tmp_path):
    root = build_foundry(tmp_path, corrupt="sec_company_tickers_exchange.jsonl")
    source = FoundryDataSource(root=root)
    with pytest.raises(FoundryError, match="sha256 mismatch"):
        source.universe_tickers()


def test_unlisted_file_refused(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    with pytest.raises(FoundryError, match="not listed"):
        source._read_dataset_file("data/symbols/current", "evil.jsonl")


def test_path_escape_refused(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    with pytest.raises(FoundryError):
        source._read_bytes("../outside.txt")


def test_dividends_and_splits_roundtrip(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    dividends = source.dividends()
    assert set(dividends["ticker"]) == {"AAPL"}
    splits = source.splits()
    assert splits.iloc[0]["ratio"] == 4.0
    assert splits.iloc[0]["effective_date"] == pd.Timestamp("2020-08-28")


def test_trailing_dps_sums_recent_quarters_only(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    # Two 0.25 quarters inside the window; the 2019 quarter and the annual
    # row are both excluded (window + span filters).
    assert source.trailing_dps("AAPL") == pytest.approx(0.50)
    assert source.trailing_dps("MSFT") is None  # absent ticker -> unknown, not zero


def test_requires_exactly_one_access_mode(tmp_path):
    with pytest.raises(ValueError):
        FoundryDataSource()
    with pytest.raises(ValueError):
        FoundryDataSource(root=tmp_path, url_base="https://example.com")


def test_cli_universe_foundry_prefix(tmp_path, monkeypatch):
    from stock_grader.cli import _load_universe

    build_foundry(tmp_path)
    tickers = _load_universe(f"foundry:{tmp_path}")
    assert tickers == ["AAPL", "BRK-B"]


def test_universe_asof_replays_events_backward(tmp_path):
    # NEWCO was added 07-25 and DEADCO removed 07-20. As of 07-19 (the day
    # before the removal, provably within the archive) DEADCO was still alive
    # and NEWCO not yet listed; as of 07-22 both events' outcomes differ.
    source = FoundryDataSource(root=build_foundry(tmp_path))
    tickers = source.universe_tickers(asof="2026-07-19")
    assert "NEWCO" not in tickers
    assert "DEADCO" in tickers
    assert "AAPL" in tickers
    # As of 07-22: DEADCO already dead, NEWCO still unlisted.
    mid = source.universe_tickers(asof="2026-07-22")
    assert "DEADCO" not in mid and "NEWCO" not in mid
    # As of 07-26 (after both events) membership matches current.
    assert "DEADCO" not in source.universe_tickers(asof="2026-07-26")


def test_universe_asof_before_archive_refuses(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    with pytest.raises(FoundryError, match="predates the event archive"):
        source.universe_tickers(asof="2026-01-01")
