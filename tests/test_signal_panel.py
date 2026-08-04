"""The signal-panel return join: one owner, one split table, honest attestations.

The headline test is :func:`test_planted_three_for_two_split_no_longer_fabricates_a_loss`.
The vault's retired copy of this chain carried split ratios (2, 3, 4, 5, 6, 7,
8, 10, 15, 20) and NOT 1.5, so a 3:2 forward split — price x 2/3 — matched
nothing, tripped no guard, and became a fabricated ~-33% forward return in a
panel that fed the evaluator. That bug is what motivated moving the
computation; this file is the proof it cannot come back.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import pandas as pd
import pytest
from tests.test_vault import _gz_jsonl, _manifest

from stock_grader.backtest import BacktestConfig, evaluate_walk_forward
from stock_grader.data.vault import VaultDataSource, VaultError
from stock_grader.panel import PLAUSIBLE_SPLIT_RATIOS
from stock_grader.signal_panel import (
    SIGNAL_PANEL_VERSION,
    SignalPanelConfig,
    SignalPanelError,
    build_signal_panel,
    write_signal_panel,
)

#: The ratios the RETIRED vault copy knew. 1.5 and 2.5 were never in it.
RETIRED_VAULT_SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0, 15.0, 20.0)

SIGNAL = dt.date(2026, 8, 3)
ENTRY = dt.date(2026, 8, 4)
EXIT = dt.date(2026, 8, 7)
SPLIT_DAY = dt.date(2026, 8, 5)

HEALTHY = ["ALPHA", "BETA", "GAMMA", "DELTA", "EPSCO", "ZETA", "ETACO", "THETA"]


def _weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


DAYS = _weekdays(dt.date(2026, 8, 3), dt.date(2026, 8, 14))


def _bar(symbol: str, close: float, volume: float = 1e5, transactions: float = 1000.0) -> dict:
    return {
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": round(close, 4),
        "volume": volume,
        "vwap": close,
        "transactions": transactions,
    }


def _build_vault_root(root: Path) -> Path:
    """market_eod, the delisted cohort archive, and one month of dividends."""
    by_month: dict[str, dict[str, list[dict]]] = {}
    for index, day in enumerate(DAYS):
        rows = [_bar(name, 100.0 + index) for name in HEALTHY]
        # SPLIT32: a 3:2 forward split on 08-05. Price x 2/3, share volume
        # x 1.5, trade count flat — the exact signature of a split, and the
        # exact shape the retired vault table could not see.
        if day < SPLIT_DAY:
            rows.append(_bar("SPLIT32", 150.0))
        else:
            rows.append(_bar("SPLIT32", 100.0, volume=1.5e5, transactions=1000.0))
        # DIVCO: flat price, one $2.00 cash dividend going ex inside the window.
        rows.append(_bar("DIVCO", 100.0))
        # DEADCO: stops trading after 08-05; the cohort archive has its last trade.
        if day <= SPLIT_DAY:
            rows.append(_bar("DEADCO", 10.0))
        # LOSTCO: an entry bar and nothing after it, anywhere: unresolvable.
        if day == ENTRY:
            rows.append(_bar("LOSTCO", 5.0))
        # NOENTRY: trades before and after the window but not on the entry day.
        if day != ENTRY:
            rows.append(_bar("NOENTRY", 42.0))
        by_month.setdefault(day.strftime("%Y-%m"), {})[day.isoformat()] = rows

    for month, files in by_month.items():
        directory = root / "data" / "market_eod" / month
        directory.mkdir(parents=True, exist_ok=True)
        names = []
        for iso, rows in files.items():
            name = f"{iso}.jsonl.gz"
            (directory / name).write_bytes(_gz_jsonl(rows))
            names.append(name)
        _manifest(directory, sorted(names))

    dead = root / "data" / "delisted_prices" / "2026"
    dead.mkdir(parents=True, exist_ok=True)
    history = {
        "data": [{"t": "2026-08-06", "o": 9.0, "h": 9.2, "l": 8.0, "c": 8.0, "a": 4.0, "v": 900}],
        "status": "delisted",
    }
    (dead / "DEADCO.json.gz").write_bytes(gzip.compress(json.dumps(history).encode()))
    _manifest(dead, ["DEADCO.json.gz"])

    dividends = root / "data" / "dividends" / "2026-08"
    dividends.mkdir(parents=True, exist_ok=True)
    (dividends / "2026-08.jsonl.gz").write_bytes(
        _gz_jsonl(
            [
                {
                    "ticker": "DIVCO",
                    "ex_dividend_date": "2026-08-06",
                    "cash_amount": 2.0,
                    "currency": "USD",
                    "dividend_type": "CD",
                }
            ]
        )
    )
    _manifest(dividends, ["2026-08.jsonl.gz"])
    return root


def _observation(
    ticker: str,
    signal_raw: float,
    score: float,
    *,
    signal: dt.date = SIGNAL,
    entry: dt.date = ENTRY,
    exit_: dt.date = EXIT,
    membership_is_pit: bool = True,
) -> dict:
    return {
        "signal_date": signal.isoformat(),
        "return_start": entry.isoformat(),
        "return_end": exit_.isoformat(),
        "ticker": ticker,
        "cik": str(abs(hash(ticker)) % 10**9).zfill(10),
        "security_id": None,
        "signal_raw": signal_raw,
        "filed_through": signal.isoformat(),
        "source_asof": signal.isoformat(),
        "membership_is_pit": membership_is_pit,
        "signal_name": "unit_signal",
        "signal_direction": -1,
        "overlapping_windows": False,
        "horizon_trading_days": 0,
        "vault_commit": "cafe123",
        "schema_version": "2.0",
        "panel_version": SIGNAL_PANEL_VERSION,
        "score": score,
    }


def _write_observations(
    root: Path,
    signal_name: str,
    parts: dict[dt.date, list[dict]],
    *,
    corrupt: str | None = None,
    version: int = SIGNAL_PANEL_VERSION,
) -> Path:
    directory = root / "data" / "signal_panels" / signal_name / f"v{version}" / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for day, rows in sorted(parts.items()):
        name = f"{day.isoformat()}.parquet"
        pd.DataFrame(rows).to_parquet(directory / name, index=False)
        names.append(name)
    _manifest(directory, sorted(names), corrupt=corrupt)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest.update(
        {
            "signal": signal_name,
            "artifact": "raw_observations",
            "direction": -1,
            "periods_per_year": 24,
            "horizon": "next-observation",
            "license_note": "unit fixture; private",
        }
    )
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def _hazard_rows(**kwargs) -> list[dict]:
    rows = [
        _observation(name, float(index), float(index) / 10.0, **kwargs)
        for index, name in enumerate(HEALTHY, start=1)
    ]
    rows.append(_observation("SPLIT32", 20.0, 0.9, **kwargs))
    rows.append(_observation("DIVCO", 21.0, 0.91, **kwargs))
    rows.append(_observation("DEADCO", 22.0, 0.92, **kwargs))
    rows.append(_observation("LOSTCO", 23.0, 0.93, **kwargs))
    rows.append(_observation("NOENTRY", 24.0, 0.94, **kwargs))
    return rows


@pytest.fixture()
def vault_root(tmp_path: Path) -> Path:
    return _build_vault_root(tmp_path / "vault")


def _build(root: Path, signal_name: str = "unit_signal", **config_kwargs):
    vault = VaultDataSource(root)
    result, parts = build_signal_panel(
        vault, signal_name, config=SignalPanelConfig(**config_kwargs)
    )
    panel_path = write_signal_panel(vault, signal_name, result, parts)
    return result, panel_path


# -- the bug this migration exists to kill ------------------------------------


def test_the_retired_vault_table_could_not_see_a_three_for_two_split():
    """Documents the divergence, so the fix cannot be quietly undone."""
    assert 1.5 not in RETIRED_VAULT_SPLIT_RATIOS
    assert 2.5 not in RETIRED_VAULT_SPLIT_RATIOS
    assert 1.5 in PLAUSIBLE_SPLIT_RATIOS
    assert 2.5 in PLAUSIBLE_SPLIT_RATIOS


def test_planted_three_for_two_split_no_longer_fabricates_a_loss(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(vault_root)
    panel = pd.read_parquet(panel_path)
    row = panel[panel["ticker"] == "SPLIT32"].iloc[0]

    # The unadjusted price move IS the fabricated loss the vault used to record.
    assert row["start_close"] == pytest.approx(150.0)
    assert row["end_close"] == pytest.approx(100.0)
    assert row["end_close"] / row["start_close"] - 1.0 == pytest.approx(-1.0 / 3.0)

    # The single owner adjusts instead of fabricating (or dropping).
    assert row["split_factor"] == pytest.approx(1.5)
    assert row["split_source"] == "reconstructed"
    assert row["forward_return"] == pytest.approx(0.0)
    assert result.periods[0].split_adjusted_reconstructed == 1


def test_a_split_shaped_move_without_corroboration_is_excluded_not_fabricated(vault_root):
    """A crash is not a split: uncorroborated, so the row is dropped AND counted."""
    root = vault_root
    for day in DAYS:
        month = root / "data" / "market_eod" / day.strftime("%Y-%m")
        name = f"{day.isoformat()}.jsonl.gz"
        rows = [
            json.loads(line)
            for line in gzip.decompress((month / name).read_bytes()).decode().splitlines()
            if line.strip()
        ]
        # CRASHCO halves on the split day with volume AND transactions spiking.
        if day < SPLIT_DAY:
            rows.append(_bar("CRASHCO", 20.0, volume=5e4, transactions=200.0))
        else:
            rows.append(_bar("CRASHCO", 10.0, volume=1e6, transactions=4000.0))
        (month / name).write_bytes(_gz_jsonl(rows))
    for month_dir in (root / "data" / "market_eod").iterdir():
        _manifest(month_dir, sorted(p.name for p in month_dir.glob("*.jsonl.gz")))

    rows = _hazard_rows()
    rows.append(_observation("CRASHCO", 30.0, 0.95))
    _write_observations(root, "unit_signal", {SIGNAL: rows})
    result, panel_path = _build(root)
    panel = pd.read_parquet(panel_path)
    assert "CRASHCO" not in set(panel["ticker"])
    assert "CRASHCO" in result.unresolved_tickers
    assert result.attestations["universe_is_pit"] is False


# -- total returns and the delisting chain ------------------------------------


def test_in_window_cash_dividend_lands_in_the_forward_return(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    _, panel_path = _build(vault_root)
    row = pd.read_parquet(panel_path).query("ticker == 'DIVCO'").iloc[0]
    assert row["dividend_covered"]
    assert row["dividend_cash"] == pytest.approx(2.0)
    assert row["dividend_count"] == 1
    # Price flat at 100, $2 of cash: a price-only chain would have said 0.0.
    assert row["forward_return"] == pytest.approx(0.02)


def test_delisted_cohort_archive_supplies_the_exit_price(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(vault_root)
    row = pd.read_parquet(panel_path).query("ticker == 'DEADCO'").iloc[0]
    assert row["return_source"] == "delisted_archive"
    assert row["forward_return"] == pytest.approx(8.0 / 10.0 - 1.0)
    assert result.periods[0].resolved_delisted_archive == 1


def test_entry_side_gap_and_unresolvable_exit_are_counted_apart(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(vault_root)
    tickers = set(pd.read_parquet(panel_path)["ticker"])
    assert "NOENTRY" not in tickers  # knowable at entry
    assert "LOSTCO" not in tickers  # outcome-dependent
    accounting = result.periods[0]
    assert accounting.no_start_price_dropped == 1
    assert accounting.unresolved_dropped == 1
    assert result.unresolved_tickers == ["LOSTCO"]
    assert (
        accounting.observations
        == accounting.kept + accounting.no_start_price_dropped + accounting.unresolved_dropped
    )


# -- attestations are computed, never declared --------------------------------


def test_attestations_are_false_while_any_leg_is(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(vault_root)
    # LOSTCO is an outcome-dependent drop, so PIT cannot hold whatever the
    # membership coverage says, and neither can the delisting attestation.
    assert result.attestations["universe_is_pit"] is False
    assert result.attestations["delisting_return_included"] is False
    assert result.unresolved_rows == 1
    assert result.pit_membership_coverage == pytest.approx(1.0)
    panel = pd.read_parquet(panel_path)
    assert not panel["universe_is_pit"].any()
    assert not panel["delisting_return_included"].any()


def test_universe_is_pit_flips_only_with_full_coverage_and_no_outcome_drops(vault_root):
    clean = [_observation(name, float(i), float(i) / 10.0) for i, name in enumerate(HEALTHY, 1)]
    _write_observations(vault_root, "clean_signal", {SIGNAL: clean})
    result, _ = _build(vault_root, "clean_signal")
    assert result.unresolved_rows == 0
    assert result.pit_membership_coverage == pytest.approx(1.0)
    assert result.attestations["universe_is_pit"] is True
    assert result.attestations["delisting_return_included"] is True


def test_one_pre_boundary_row_holds_the_panel_attestation_false(vault_root):
    rows = [_observation(name, float(i), float(i) / 10.0) for i, name in enumerate(HEALTHY, 1)]
    rows[0]["membership_is_pit"] = False  # a signal date the pit tables cannot prove
    _write_observations(vault_root, "mixed_signal", {SIGNAL: rows})
    result, _ = _build(vault_root, "mixed_signal")
    assert result.unresolved_rows == 0
    assert result.pit_membership_coverage == pytest.approx(7 / 8)
    assert result.attestations["universe_is_pit"] is False


def test_return_is_total_needs_the_coverage_bar_not_a_flag(vault_root):
    # Every kept row is DIVCO-like: flat price, covered window, so coverage is 1.0.
    rows = [
        _observation("DIVCO", 1.0, 0.1),
        *[_observation(name, float(i), float(i) / 10.0) for i, name in enumerate(HEALTHY, 2)],
    ]
    _write_observations(vault_root, "div_signal", {SIGNAL: rows})
    result, _ = _build(vault_root, "div_signal")
    assert result.dividend_coverage == pytest.approx(1.0)
    assert result.attestations["return_is_total"] is True


def test_a_vault_without_the_dividend_archive_stays_price_only(tmp_path):
    import shutil

    root = _build_vault_root(tmp_path / "vault")
    shutil.rmtree(root / "data" / "dividends")
    _write_observations(root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(root)
    assert result.dividend_archive_months == 0
    assert result.attestations["return_is_total"] is False
    row = pd.read_parquet(panel_path).query("ticker == 'DIVCO'").iloc[0]
    assert row["forward_return"] == pytest.approx(0.0)


# -- pre-registration: the score is never touched here ------------------------


def test_the_observed_score_is_carried_through_untouched(vault_root):
    rows = _hazard_rows()
    _write_observations(vault_root, "unit_signal", {SIGNAL: rows})
    _, panel_path = _build(vault_root)
    panel = pd.read_parquet(panel_path)
    observed = {row["ticker"]: row["score"] for row in rows}
    for _, joined in panel.iterrows():
        assert joined["score"] == pytest.approx(observed[joined["ticker"]])
    # And dropping LOSTCO/NOENTRY did NOT re-rank anyone: the vault ranked the
    # observation cross-section before any return existed.
    assert sorted(panel["score"]) == sorted(
        observed[t] for t in panel["ticker"]
    )


# -- artifact contract --------------------------------------------------------


def test_observation_parts_are_sha256_verified(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()}, corrupt=None)
    directory = (
        vault_root / "data" / "signal_panels" / "unit_signal"
        / f"v{SIGNAL_PANEL_VERSION}" / "observations"
    )
    manifest = json.loads((directory / "manifest.json").read_text())
    for entry in manifest["files"]:
        entry["sha256"] = "0" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(VaultError, match="sha256 mismatch"):
        _build(vault_root)


def test_a_part_that_already_carries_returns_is_refused(vault_root):
    rows = [dict(row, forward_return=0.1) for row in _hazard_rows()]
    _write_observations(vault_root, "unit_signal", {SIGNAL: rows})
    with pytest.raises(SignalPanelError, match="already carries forward_return"):
        _build(vault_root)


def test_a_part_missing_a_required_column_is_refused(vault_root):
    rows = [{k: v for k, v in row.items() if k != "membership_is_pit"} for row in _hazard_rows()]
    _write_observations(vault_root, "unit_signal", {SIGNAL: rows})
    with pytest.raises(SignalPanelError, match="missing required column"):
        _build(vault_root)


def test_a_part_mixing_return_windows_is_refused(vault_root):
    rows = _hazard_rows()
    rows[0]["return_end"] = dt.date(2026, 8, 6).isoformat()
    _write_observations(vault_root, "unit_signal", {SIGNAL: rows})
    with pytest.raises(SignalPanelError, match="mixes return windows"):
        _build(vault_root)


def test_a_zero_length_return_window_is_refused(vault_root):
    """The producer refuses to emit these; the single owner refuses to price
    one. A window whose exit day IS its entry day measures a close against
    itself: every forward return is exactly 0.0, and under v6's computed
    attestations that table of zeros would attest perfectly."""
    rows = [
        _observation(name, float(i), float(i) / 10.0, entry=ENTRY, exit_=ENTRY)
        for i, name in enumerate(HEALTHY, start=1)
    ]
    _write_observations(vault_root, "unit_signal", {SIGNAL: rows})
    with pytest.raises(SignalPanelError, match="zero-length return window"):
        _build(vault_root)


def test_the_path_guard_refuses_to_write_outside_the_vault(vault_root):
    from stock_grader import signal_panel

    vault = VaultDataSource(vault_root)
    with pytest.raises(SignalPanelError, match="escapes the vault"):
        signal_panel._panel_dir(vault, "../../../escape", SIGNAL_PANEL_VERSION)


# -- immutability, incremental runs, and the written layout -------------------


def test_parts_are_immutable_and_whole_panel_accounting_survives_a_rerun(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    first, panel_path = _build(vault_root)
    part = panel_path.parent / f"{SIGNAL.isoformat()}.parquet"
    stamp = part.stat().st_mtime_ns
    before = pd.read_parquet(panel_path)

    second, panel_path_again = _build(vault_root)
    assert part.stat().st_mtime_ns == stamp  # never rewritten
    assert second.parts_written == 0
    # Whole-panel numbers come from counts.json, so they do not collapse to the
    # (empty) work this run did.
    assert second.kept_rows == first.kept_rows
    assert second.unresolved_rows == first.unresolved_rows
    assert second.attestations == first.attestations
    assert second.pit_membership_coverage == pytest.approx(first.pit_membership_coverage)
    pd.testing.assert_frame_equal(before, pd.read_parquet(panel_path_again))


def test_the_written_layout_carries_its_catalog_and_sidecar(vault_root):
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    result, panel_path = _build(vault_root)
    directory = panel_path.parent
    assert (directory / "observations" / "manifest.json").is_file()
    build = json.loads((directory / "build.json").read_text())
    assert build["attestations"] == result.attestations
    assert build["return_semantics"]["owner"].startswith("stock_grader.signal_panel")
    assert build["whole_panel_accounting"] == "counts.json"
    counts = json.loads((directory / "counts.json").read_text())
    assert set(counts) == {SIGNAL.isoformat()}

    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["schema_version"] == "1.0"
    assert manifest["artifact"] == "evaluable_panel"
    assert manifest["spec"]["periods_per_year"] == 24
    catalogued = {entry["name"] for entry in manifest["files"]}
    assert {"panel.parquet", "build.json", "counts.json", f"{SIGNAL.isoformat()}.parquet"} <= (
        catalogued
    )
    # The catalog is the contract: every hash must match the bytes on disk.
    import hashlib

    for entry in manifest["files"]:
        blob = (directory / entry["name"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"]


def test_a_second_signal_date_appends_without_touching_the_first(vault_root):
    later_signal = dt.date(2026, 8, 10)
    later = [
        _observation(
            name,
            float(index),
            float(index) / 10.0,
            signal=later_signal,
            entry=dt.date(2026, 8, 11),
            exit_=dt.date(2026, 8, 14),
        )
        for index, name in enumerate(HEALTHY, start=1)
    ]
    _write_observations(vault_root, "unit_signal", {SIGNAL: _hazard_rows()})
    first, panel_path = _build(vault_root)
    first_part = panel_path.parent / f"{SIGNAL.isoformat()}.parquet"
    stamp = first_part.stat().st_mtime_ns

    _write_observations(
        vault_root, "unit_signal", {SIGNAL: _hazard_rows(), later_signal: later}
    )
    second, panel_path = _build(vault_root)
    assert first_part.stat().st_mtime_ns == stamp
    assert second.parts_written == 1
    assert second.kept_rows == first.kept_rows + len(HEALTHY)
    panel = pd.read_parquet(panel_path)
    assert set(panel["signal_date"]) == {SIGNAL.isoformat(), later_signal.isoformat()}


def test_the_joined_panel_satisfies_the_evaluator_contract(vault_root):
    windows = {
        SIGNAL: (ENTRY, EXIT),
        dt.date(2026, 8, 10): (dt.date(2026, 8, 11), dt.date(2026, 8, 14)),
    }
    parts = {
        signal: [
            _observation(
                name, float(i), float(i) / 10.0, signal=signal, entry=entry, exit_=exit_
            )
            for i, name in enumerate(HEALTHY, start=1)
        ]
        for signal, (entry, exit_) in windows.items()
    }
    _write_observations(vault_root, "eval_signal", parts)
    _, panel_path = _build(vault_root, "eval_signal")
    panel = pd.read_parquet(panel_path)
    report = evaluate_walk_forward(panel, BacktestConfig(min_cross_section=4, quantiles=2))
    assert report.observations == len(panel)
    assert report.input_contract["point_in_time_universe_attested"] is True
    assert report.input_contract["delistings_included_attested"] is True


def test_no_observation_dataset_is_a_clean_zero_not_a_crash(tmp_path):
    root = _build_vault_root(tmp_path / "vault")
    vault = VaultDataSource(root)
    assert vault.signal_panel_signals(SIGNAL_PANEL_VERSION) == []


def test_a_zero_row_signal_still_publishes_its_catalog(vault_root):
    _write_observations(vault_root, "empty_signal", {})
    result, panel_path = _build(vault_root, "empty_signal")
    assert result.observations == 0
    # Nothing kept: every attestation is False, never vacuously True.
    assert result.attestations == {
        "universe_is_pit": False,
        "return_is_total": False,
        "delisting_return_included": False,
    }
    assert not panel_path.is_file()
    assert (panel_path.parent / "manifest.json").is_file()
