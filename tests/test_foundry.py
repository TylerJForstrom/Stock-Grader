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
    symbols_file.write_bytes("".join(json.dumps(r) + "\n" for r in exchange_rows).encode("utf-8"))

    dividends = pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "period_start": "2025-10-01",
                "period_end": "2025-12-27",
                "span_type": "quarterly",
                "dps_current_basis": 0.25,
                "derived": False,
                "approximate": False,
                "flags": "",
            },
            {
                "ticker": "AAPL",
                "period_start": "2025-12-28",
                "period_end": "2026-03-28",
                "span_type": "quarterly",
                "dps_current_basis": 0.25,
                "derived": False,
                "approximate": False,
                "flags": "",
            },
            {
                "ticker": "AAPL",
                "period_start": "2019-01-01",
                "period_end": "2019-03-30",
                "span_type": "quarterly",
                "dps_current_basis": 0.19,
                "derived": False,
                "approximate": False,
                "flags": "",
            },  # outside any trailing window
            {
                "ticker": "AAPL",
                "period_start": "2025-01-01",
                "period_end": "2025-12-27",
                "span_type": "annual",
                "dps_current_basis": 1.0,
                "derived": False,
                "approximate": False,
                "flags": "",
            },  # annual rows must not double-count
        ]
    )
    dividends_file = actions_dir / "dividends.parquet"
    dividends.to_parquet(dividends_file, index=False)

    splits_file = actions_dir / "splits.jsonl"
    splits_file.write_bytes(
        (
            json.dumps(
                {
                    "ticker": "AAPL",
                    "effective_date": "2020-08-28",
                    "ratio": 4.0,
                    "confidence": "high",
                    "filed": "2020-10-30",
                }
            )
            + "\n"
        ).encode("utf-8")
    )

    events_dir = root / "data" / "symbols" / "events"
    events_dir.mkdir(parents=True)
    events = [
        # DEADCO delisted on 07-20 (removal event); NEWCO listed on 07-25
        {
            "date": "2026-07-20",
            "source": "sec_company_tickers_exchange",
            "event": "removed",
            "record": {"cik": 555, "ticker": "DEADCO", "title": "Dead Co", "exchange": "NYSE"},
        },
        {
            "date": "2026-07-25",
            "source": "sec_company_tickers_exchange",
            "event": "added",
            "record": {"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"},
        },
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
            json.dumps(
                {
                    "schema_version": schema,
                    "source_urls": [],
                    "license_note": "test",
                    "files": files,
                }
            )
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


def test_symbol_directory_reader_is_manifest_verified(tmp_path):
    root = build_foundry(tmp_path)
    directory = root / "data" / "symbols" / "current"
    path = directory / "nasdaqlisted.jsonl"
    rows = [
        {"ticker": "AAPL", "etf": "N", "test_issue": "N"},
        {"ticker": "QQQ", "etf": "Y", "test_issue": "N"},
    ]
    blob = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    path.write_bytes(blob)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {"name": path.name, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    source = FoundryDataSource(root=root)
    assert source.symbol_directory("nasdaqlisted.jsonl") == rows


def test_dividends_and_splits_roundtrip(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    dividends = source.dividends()
    assert set(dividends["ticker"]) == {"AAPL"}
    splits = source.splits()
    assert splits.iloc[0]["ratio"] == 4.0
    assert splits.iloc[0]["effective_date"] == pd.Timestamp("2020-08-28")


def test_dividends_parquet_is_loaded_once_per_instance(tmp_path, monkeypatch):
    """trailing_dps runs once per ticker across a universe; each call must not
    re-read, re-hash, and re-parse the parquet."""
    build_foundry(tmp_path)
    reads = 0
    original = FoundryDataSource._read_dataset_file

    def counting_read(self, dataset_dir, name):
        nonlocal reads
        if name == "dividends.parquet":
            reads += 1
        return original(self, dataset_dir, name)

    monkeypatch.setattr(FoundryDataSource, "_read_dataset_file", counting_read)
    source = FoundryDataSource(root=tmp_path)
    assert source.trailing_dps("AAPL") == pytest.approx(0.50)
    assert source.trailing_dps("MSFT") is None
    assert source.dividends() is source.dividends()
    assert reads == 1
    # Per instance, not global: a fresh source must verify its own files.
    assert FoundryDataSource(root=tmp_path).trailing_dps("AAPL") == pytest.approx(0.50)
    assert reads == 2


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


def test_explicitly_requested_foundry_fails_closed_on_contract_violation(tmp_path, monkeypatch):
    """--foundry is a request, not a hint: a hash mismatch must stop the run.

    Pre-fix, a FoundryError degraded to `foundry = None` with a console line,
    so a tampered foundry produced a panel indistinguishable from one graded
    with no foundry at all.
    """
    import json

    import pytest

    from stock_grader import cli

    dataset = tmp_path / "data" / "corporate_actions"
    dataset.mkdir(parents=True)
    (dataset / "dividends.parquet").write_bytes(b"not really parquet")
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "files": [
                    {"name": "dividends.parquet", "sha256": "0" * 64, "bytes": 18}
                ],
            }
        )
    )

    args = cli.build_parser().parse_args(
        ["grade", "AAPL", "--foundry", str(tmp_path), "--no-network", "--no-sec-prices"]
    )
    with pytest.raises(SystemExit) as excinfo:
        cli._build_snapshots(["AAPL"], args, provider=None)
    assert excinfo.value.code == 2


# -- TickerPulse sentiment mirror ----------------------------------------------


def add_sentiment(
    root: Path,
    days: dict[str, list[dict]],
    *,
    dataset: str = "ticker_trends",
    corrupt: str | None = None,
    unmanifested: str | None = None,
) -> Path:
    """Write a data/sentiment/<dataset>/ mirror with a hashed manifest."""
    directory = root / "data" / "sentiment" / dataset
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for day, rows in sorted(days.items()):
        blob = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")
        (directory / f"{day}.jsonl").write_bytes(blob)
        if day == unmanifested:
            continue
        digest = hashlib.sha256(blob).hexdigest()
        if day == corrupt:
            digest = "0" * 64
        files.append({"name": f"{day}.jsonl", "sha256": digest, "bytes": len(blob)})
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "producer": "TickerPulse",
                "source_urls": [],
                "license_note": "public aggregate counts and scores; no post text",
                "files": files,
            }
        )
    )
    return root


def test_sentiment_days_come_from_the_manifest_only(tmp_path):
    root = build_foundry(tmp_path)
    add_sentiment(
        root,
        {
            "2026-07-29": [{"ticker": "AAPL", "mentions": 10}],
            "2026-07-28": [{"ticker": "AAPL", "mentions": 8}],
            "2026-07-30": [{"ticker": "AAPL", "mentions": 9}],
        },
        unmanifested="2026-07-30",
    )
    source = FoundryDataSource(root=root)
    # Sorted; the unmanifested day is invisible by contract, not advertised.
    assert source.sentiment_days() == ["2026-07-28", "2026-07-29"]
    with pytest.raises(FoundryError, match="not listed"):
        source.sentiment_trends("2026-07-30")


def test_sentiment_trends_rows_are_hash_verified(tmp_path):
    rows = [
        {
            "ticker": "AAPL",
            "mentions": 428,
            "mentions_prev": 580,
            "bull": 35,
            "bear": 101,
            "neutral": 292,
            "sentiment_avg": -0.1193,
            "share_of_voice": 0.03502,
        },
        {"ticker": "AAOI", "mentions": 3, "mentions_prev": 69, "bull": 0, "bear": 1},
    ]
    root = add_sentiment(build_foundry(tmp_path), {"2026-08-01": rows})
    assert FoundryDataSource(root=root).sentiment_trends("2026-08-01") == rows


def test_sentiment_hash_mismatch_refused(tmp_path):
    root = add_sentiment(
        build_foundry(tmp_path),
        {"2026-08-01": [{"ticker": "AAPL", "mentions": 1}]},
        corrupt="2026-08-01",
    )
    with pytest.raises(FoundryError, match="sha256 mismatch"):
        FoundryDataSource(root=root).sentiment_trends("2026-08-01")


def test_sentiment_buckets_roundtrip(tmp_path):
    rows = [
        {
            "ticker": "AAPL",
            "bucket_start": "2026-08-01 00:00:00+00:00",
            "bucket_minutes": 60,
            "mentions": 30,
            "bull": 0,
            "bear": 15,
            "neutral": 15,
        }
    ]
    root = add_sentiment(build_foundry(tmp_path), {"2026-08-01": rows}, dataset="ticker_buckets")
    source = FoundryDataSource(root=root)
    assert source.sentiment_buckets("2026-08-01") == rows
    assert source.sentiment_days("ticker_buckets") == ["2026-08-01"]


def test_sentiment_unknown_dataset_refused(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    with pytest.raises(ValueError, match="unsupported sentiment dataset"):
        source.sentiment_days("raw_posts")


def test_sentiment_unknown_schema_version_refused(tmp_path):
    root = build_foundry(tmp_path)
    add_sentiment(root, {"2026-08-01": [{"ticker": "AAPL", "mentions": 1}]})
    directory = root / "data" / "sentiment" / "ticker_trends"
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["schema_version"] = "9.9"
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FoundryError, match="schema_version"):
        FoundryDataSource(root=root).sentiment_trends("2026-08-01")
