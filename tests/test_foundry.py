"""FoundryDataSource contract tests: manifest enforcement, hash verification,
and dataset reads against a fixture foundry tree."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from stock_grader.data.foundry import FoundryDataSource, FoundryError

#: Fixture archive boundary: earliest event 2026-07-20, minus one day.
PIT_BOUNDARY = "2026-07-19"


def build_foundry(
    root: Path, *, schema="1.0", corrupt: str | None = None, include_pit: bool = True
) -> Path:
    symbols_dir = root / "data" / "symbols" / "current"
    actions_dir = root / "data" / "corporate_actions"
    symbols_dir.mkdir(parents=True)
    actions_dir.mkdir(parents=True)

    exchange_rows = [
        {"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"},
        {"cik": 1067983, "ticker": "BRK-B", "title": "Berkshire", "exchange": "NYSE"},
        {"cik": 999999, "ticker": "SCAMCO", "title": "Pink Sheet Co", "exchange": ""},
        {"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"},
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
            {
                "ticker": "BRK-B",  # foundry stores the canonical SEC dash form
                "period_start": "2025-12-28",
                "period_end": "2026-03-28",
                "span_type": "quarterly",
                "dps_current_basis": 0.10,
                "derived": False,
                "approximate": False,
                "flags": "",
            },
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
        # DEADCO delisted 07-20; BRK-B retitled 07-22; NEWCO listed 07-25;
        # DELQ dropped from the Nasdaq directory 07-23.
        {
            "date": "2026-07-20",
            "source": "sec_company_tickers_exchange",
            "event": "removed",
            "record": {"cik": 555, "ticker": "DEADCO", "title": "Dead Co", "exchange": "NYSE"},
        },
        {
            "date": "2026-07-22",
            "source": "sec_company_tickers_exchange",
            "event": "changed",
            "record": {"cik": 1067983, "ticker": "BRK-B", "title": "Berkshire", "exchange": "NYSE"},
            "previous": {
                "cik": 1067983,
                "ticker": "BRK-B",
                "title": "Berkshire Hathaway B",
                "exchange": "NYSE",
            },
        },
        {
            "date": "2026-07-25",
            "source": "sec_company_tickers_exchange",
            "event": "added",
            "record": {"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"},
        },
        {
            "date": "2026-07-23",
            "source": "nasdaqlisted",
            "event": "removed",
            "record": {"ticker": "DELQ", "name": "Delisted Co", "etf": "N", "test_issue": "N"},
        },
    ]
    events_file = events_dir / "events.jsonl"
    events_file.write_bytes("".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))

    # The published pit interval tables: what Stock-Data's producer-side replay
    # emits for exactly the current/ + events/ above. Hand-written here — the
    # conformance test below re-derives membership by replaying events and
    # fails if these rows ever disagree with the stream.
    pit_dir = root / "data" / "symbols" / "pit"
    pit_dir.mkdir(parents=True)
    pit_tables = {
        "sec_company_tickers_exchange.jsonl": [
            {
                **{"cik": 320193, "ticker": "AAPL", "title": "Apple Inc.", "exchange": "Nasdaq"},
                "valid_from": PIT_BOUNDARY,
                "valid_to": None,
                "provable_from": False,
            },
            {
                **{
                    "cik": 1067983,
                    "ticker": "BRK-B",
                    "title": "Berkshire Hathaway B",
                    "exchange": "NYSE",
                },
                "valid_from": PIT_BOUNDARY,
                "valid_to": "2026-07-22",
                "provable_from": False,
            },
            {
                **{"cik": 1067983, "ticker": "BRK-B", "title": "Berkshire", "exchange": "NYSE"},
                "valid_from": "2026-07-22",
                "valid_to": None,
                "provable_from": True,
            },
            {
                **{"cik": 999999, "ticker": "SCAMCO", "title": "Pink Sheet Co", "exchange": ""},
                "valid_from": PIT_BOUNDARY,
                "valid_to": None,
                "provable_from": False,
            },
            {
                **{"cik": 555, "ticker": "DEADCO", "title": "Dead Co", "exchange": "NYSE"},
                "valid_from": PIT_BOUNDARY,
                "valid_to": "2026-07-20",
                "provable_from": False,
            },
            {
                **{"cik": 777, "ticker": "NEWCO", "title": "New Co", "exchange": "Nasdaq"},
                "valid_from": "2026-07-25",
                "valid_to": None,
                "provable_from": True,
            },
        ],
        "nasdaqlisted.jsonl": [
            {
                **{"ticker": "AAPL", "name": "Apple", "etf": "N", "test_issue": "N"},
                "valid_from": PIT_BOUNDARY,
                "valid_to": None,
                "provable_from": False,
            },
            {
                **{"ticker": "DELQ", "name": "Delisted Co", "etf": "N", "test_issue": "N"},
                "valid_from": PIT_BOUNDARY,
                "valid_to": "2026-07-23",
                "provable_from": False,
            },
        ],
    }
    pit_files = []
    for name, rows in pit_tables.items():
        (pit_dir / name).write_bytes(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode("utf-8")
        )
        pit_files.append(name)

    for directory, names, extra in (
        (symbols_dir, [symbols_file.name], {}),
        (actions_dir, [dividends_file.name, splits_file.name], {}),
        (events_dir, [events_file.name], {}),
        (pit_dir, pit_files if include_pit else [], {"reconstructable_from": PIT_BOUNDARY}),
    ):
        if directory is pit_dir and not include_pit:
            for name in pit_files:
                (pit_dir / name).unlink()
            pit_dir.rmdir()
            continue
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
                    **extra,
                }
            )
        )
    return root


def test_universe_filters_unlisted_venues(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    tickers = source.universe_tickers()
    assert tickers == ["AAPL", "BRK-B", "NEWCO"]  # SCAMCO (no listed exchange) excluded
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
    assert set(dividends["ticker"]) == {"AAPL", "BRK-B"}
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


def test_trailing_dps_bridges_ticker_spellings(tmp_path):
    """The foundry stores SEC dash form; dot and space callers still resolve."""
    source = FoundryDataSource(root=build_foundry(tmp_path))
    assert source.trailing_dps("BRK-B") == pytest.approx(0.10)
    assert source.trailing_dps("BRK.B") == pytest.approx(0.10)
    assert source.trailing_dps("BRK B") == pytest.approx(0.10)


def test_requires_exactly_one_access_mode(tmp_path):
    with pytest.raises(ValueError):
        FoundryDataSource()
    with pytest.raises(ValueError):
        FoundryDataSource(root=tmp_path, url_base="https://example.com")


def test_cli_universe_foundry_prefix(tmp_path, monkeypatch):
    from stock_grader.cli import _load_universe

    build_foundry(tmp_path)
    tickers = _load_universe(f"foundry:{tmp_path}")
    assert tickers == ["AAPL", "BRK-B", "NEWCO"]


def test_universe_asof_is_a_pit_interval_lookup(tmp_path):
    # NEWCO was added 07-25 and DEADCO removed 07-20. As of 07-19 (the archive
    # boundary) DEADCO was still alive and NEWCO not yet listed; as of 07-22
    # both events' outcomes differ.
    source = FoundryDataSource(root=build_foundry(tmp_path))
    tickers = source.universe_tickers(asof="2026-07-19")
    assert "NEWCO" not in tickers
    assert "DEADCO" in tickers
    assert "AAPL" in tickers
    # As of 07-22: DEADCO already dead, NEWCO still unlisted.
    mid = source.universe_tickers(asof="2026-07-22")
    assert "DEADCO" not in mid and "NEWCO" not in mid
    # As of 07-26 (after both events) membership matches current.
    late = source.universe_tickers(asof="2026-07-26")
    assert "DEADCO" not in late and "NEWCO" in late
    # The interval bookkeeping fields never leak into directory records.
    for record in source.universe(listed_only=False, asof="2026-07-22"):
        assert not {"valid_from", "valid_to", "provable_from"} & set(record)


def _replay_members(current, events, source_key, asof, key_fields):
    """Reference implementation: the retired consumer-side backward replay.

    Kept ONLY as an executable specification for the conformance test below —
    production code answers asof queries from the published interval table.
    """

    def key(record):
        return tuple(record.get(f) for f in key_fields)

    members = {key(r): r for r in current}
    replayed = [
        e for e in events if e.get("source") == source_key and str(e.get("date", "")) > asof
    ]
    for event in sorted(replayed, key=lambda e: str(e.get("date", "")), reverse=True):
        record = event.get("record") or {}
        kind = event.get("event")
        if kind == "added":
            members.pop(key(record), None)
        elif kind == "removed":
            members[key(record)] = record
        elif kind == "changed" and event.get("previous"):
            members[key(record)] = event["previous"]
    return members


def _canon(rows):
    return {json.dumps(r, sort_keys=True) for r in rows}


def test_pit_table_slice_equals_replayed_universe_at_every_event_date(tmp_path):
    """The migration's burn-in gate: lookup == replay, at and between all dates.

    universe(asof=D) now slices the published interval table; this replays the
    same event stream backward — the algorithm the table retired — and demands
    identical membership on every date of the fixture window, covering added,
    removed, AND changed events plus the boundary and between-event dates.
    """
    source = FoundryDataSource(root=build_foundry(tmp_path))
    current = source.symbol_directory("sec_company_tickers_exchange.jsonl")
    events = source.events()
    for day in range(19, 28):
        asof = f"2026-07-{day:02d}"
        replayed = _replay_members(
            current, events, "sec_company_tickers_exchange", asof, ("cik", "ticker")
        )
        looked_up = source.universe(listed_only=False, asof=asof)
        assert _canon(looked_up) == _canon(replayed.values()), asof


def test_symbol_directory_asof_is_a_pit_interval_lookup(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    alive = {r["ticker"] for r in source.symbol_directory("nasdaqlisted.jsonl", asof="2026-07-20")}
    assert alive == {"AAPL", "DELQ"}
    after = {r["ticker"] for r in source.symbol_directory("nasdaqlisted.jsonl", asof="2026-07-24")}
    assert after == {"AAPL"}  # DELQ's interval closed on the 07-23 removal


def test_universe_asof_before_archive_refuses(tmp_path):
    source = FoundryDataSource(root=build_foundry(tmp_path))
    with pytest.raises(FoundryError, match="predates the event archive"):
        source.universe_tickers(asof="2026-01-01")


def test_universe_asof_without_pit_dataset_refuses(tmp_path):
    """No silent fallback: an asof query on a foundry lacking the pit tables
    must refuse loudly, never quietly serve today's snapshot as history."""
    source = FoundryDataSource(root=build_foundry(tmp_path, include_pit=False))
    with pytest.raises(FoundryError, match="missing foundry file"):
        source.universe_tickers(asof="2026-07-22")
    assert source.universe_tickers() == ["AAPL", "BRK-B", "NEWCO"]  # current still readable


def test_universe_asof_pit_hash_mismatch_refuses(tmp_path):
    root = build_foundry(tmp_path)
    pit_file = root / "data" / "symbols" / "pit" / "sec_company_tickers_exchange.jsonl"
    pit_file.write_bytes(pit_file.read_bytes() + b"\n")
    source = FoundryDataSource(root=root)
    with pytest.raises(FoundryError, match="sha256 mismatch"):
        source.universe_tickers(asof="2026-07-22")


def test_universe_asof_pit_manifest_without_boundary_refuses(tmp_path):
    root = build_foundry(tmp_path)
    manifest_path = root / "data" / "symbols" / "pit" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["reconstructable_from"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = FoundryDataSource(root=root)
    with pytest.raises(FoundryError, match="reconstructable_from"):
        source.universe_tickers(asof="2026-07-22")


def test_universe_asof_malformed_pit_row_refuses(tmp_path):
    root = build_foundry(tmp_path)
    pit_dir = root / "data" / "symbols" / "pit"
    pit_file = pit_dir / "sec_company_tickers_exchange.jsonl"
    blob = (json.dumps({"cik": 1, "ticker": "X", "valid_from": "2026-07-19"}) + "\n").encode()
    pit_file.write_bytes(blob)
    manifest_path = pit_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["name"] == pit_file.name:
            entry["sha256"] = hashlib.sha256(blob).hexdigest()
            entry["bytes"] = len(blob)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = FoundryDataSource(root=root)
    with pytest.raises(FoundryError, match="malformed pit row"):
        source.universe_tickers(asof="2026-07-22")


@pytest.mark.skipif(
    "STOCK_DATA_ROOT" not in os.environ,
    reason="set STOCK_DATA_ROOT to a Stock-Data checkout to run the real-archive gate",
)
def test_real_archive_pit_lookup_equals_replay_at_every_event_date():
    """Real-archive burn-in: table slice == replayed universe(asof=D) for every
    event date in the actual foundry archive (plus its boundary). Run locally
    against a Stock-Data checkout before trusting a migration; CI covers the
    same property with fixtures here and with the real archive in Stock-Data's
    own suite."""
    source = FoundryDataSource(root=os.environ["STOCK_DATA_ROOT"])
    current = source.symbol_directory("sec_company_tickers_exchange.jsonl")
    events = source.events()
    boundary = source.manifest("data/symbols/pit")["reconstructable_from"]
    dates = sorted(
        {boundary}
        | {
            str(e["date"])
            for e in events
            if e.get("source") == "sec_company_tickers_exchange"
        }
    )
    assert dates, "real archive unexpectedly empty"
    for asof in dates:
        replayed = _replay_members(
            current, events, "sec_company_tickers_exchange", asof, ("cik", "ticker")
        )
        looked_up = source.universe(listed_only=False, asof=asof)
        assert _canon(looked_up) == _canon(replayed.values()), f"diverges at {asof}"


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
