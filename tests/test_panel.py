"""Forward panel builder: contract, splits, delistings, gates, and honesty."""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

import pandas as pd
import pytest
from tests.test_vault import _gz_jsonl, _manifest

from stock_grader.backtest import BacktestConfig, evaluate_walk_forward
from stock_grader.data.vault import VaultDataSource
from stock_grader.panel import (
    FORWARD_EPOCH,
    PanelBuildConfig,
    PanelBuildError,
    build_panel,
    detect_split,
    select_non_overlapping,
    window_for,
    write_panel,
)

HORIZON = 5
TODAY = dt.date(2026, 9, 11)
SIGNALS = (dt.date(2026, 8, 3), dt.date(2026, 8, 12), dt.date(2026, 8, 21))


def _weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    days, cursor = [], start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


DAYS = _weekdays(dt.date(2026, 8, 3), dt.date(2026, 9, 11))

# Deterministic healthy names: enough for the backtest's quantile floor.
HEALTHY = {
    "ALPHA": (100.0, 0.30),
    "BETA": (50.0, -0.10),
    "GAMMA": (75.0, 0.20),
    "DELTA": (30.0, 0.05),
    "EPSCO": (60.0, -0.05),
    "ZETA": (45.0, 0.15),
    "ETACO": (25.0, 0.02),
    "THETA": (90.0, -0.20),
}


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


def _build_market_vault(root: Path) -> Path:
    """A month-long fixture vault with split, delisting, and spelling hazards."""
    by_month: dict[str, dict[str, list[dict]]] = {}
    for index, day in enumerate(DAYS):
        rows: list[dict] = []
        for ticker, (base, drift) in HEALTHY.items():
            rows.append(_bar(ticker, base + drift * index))
        rows.append(_bar("BRK.B", 470.0 + 0.2 * index))  # panel says BRK-B
        if day <= dt.date(2026, 8, 6):
            rows.append(_bar("DEADCO", 10.0))  # delists mid-window, has cohort history
            rows.append(_bar("GONECO", 20.0 + 0.1 * index))  # vanishes, no history
        if day == dt.date(2026, 8, 4):
            rows.append(_bar("LOSTCO", 5.0))  # entry bar only: unresolvable
        # SPLITF: 2-for-1 on 08-06, confirmed by a foundry row.
        if day < dt.date(2026, 8, 6):
            rows.append(_bar("SPLITF", 100.0))
        else:
            rows.append(_bar("SPLITF", 50.0 + 0.05 * index, volume=2e5, transactions=1100.0))
        # SPLITR: same halving, no foundry row, volume doubles, transactions flat.
        if day < dt.date(2026, 8, 6):
            rows.append(_bar("SPLITR", 80.0))
        else:
            rows.append(_bar("SPLITR", 40.0 + 0.02 * index, volume=2e5, transactions=1000.0))
        # CRASHCO: halving with volume AND transactions spiking — a crash, not a split.
        if day < dt.date(2026, 8, 6):
            rows.append(_bar("CRASHCO", 20.0, volume=5e4, transactions=200.0))
        else:
            rows.append(_bar("CRASHCO", 10.0 - 0.01 * index, volume=1e6, transactions=4000.0))
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
        "data": [
            {"t": "2026-08-07", "o": 9.0, "h": 9.2, "l": 8.5, "c": 8.0, "a": 4.0, "v": 900},
            {"t": "2026-08-10", "o": 8.0, "h": 8.1, "l": 7.2, "c": 7.5, "a": 3.75, "v": 400},
        ],
        "status": "delisted",
    }
    (dead / "DEADCO.json.gz").write_bytes(gzip.compress(json.dumps(history).encode()))
    (dead / "cohort_index.json").write_text("{}")
    _manifest(dead, ["DEADCO.json.gz", "cohort_index.json"])
    return root


_CIKS = {
    ticker: str(2_000_000 + i).zfill(10)
    for i, ticker in enumerate(
        [*HEALTHY, "BRK-B", "DEADCO", "GONECO", "LOSTCO", "SPLITF", "SPLITR", "CRASHCO", "NOPE"]
    )
}


def _frozen_row(ticker: str, score: float, *, graded: bool = True, cik: str | None = None) -> dict:
    return {
        "ticker": ticker,
        "cik": _CIKS[ticker] if cik is None else cik,
        "score": score,
        "letter": "B",
        "percentile": 50.0,
        "coverage": 0.9,
        "graded": graded,
        "profile": "all_weather",
        "config_fingerprint": "cfg",
        "universe_fingerprint": "uni",
        "code_commit": "deadbeef",
        "schema_version": "1.0",
    }


def _write_frozen(root: Path, signal: dt.date, rows: list[dict], profile: str = "all_weather"):
    directory = root / profile
    directory.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([{"signal_date": signal.isoformat(), **row} for row in rows])
    frame.to_parquet(directory / f"{signal.isoformat()}.parquet", index=False)


def _healthy_rows() -> list[dict]:
    return [
        _frozen_row(ticker, score)
        for score, ticker in enumerate(sorted(HEALTHY), start=1)
    ]


class _Foundry:
    """Split confirmations and CIK enrichment, shaped like FoundryDataSource."""

    def splits(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ticker": "SPLITF",
                    "cik": _CIKS["SPLITF"],
                    "effective_date": pd.Timestamp("2026-08-06"),
                    "ratio": 2.0,
                    "confidence": "high",
                }
            ]
        )

    def universe(self, *, listed_only: bool = False):
        return [{"ticker": t, "cik": int(c)} for t, c in _CIKS.items()]


@pytest.fixture(scope="module")
def market_vault(tmp_path_factory) -> VaultDataSource:
    root = _build_market_vault(tmp_path_factory.mktemp("vault"))
    return VaultDataSource(root)


@pytest.fixture()
def frozen_root(tmp_path: Path) -> Path:
    root = tmp_path / "frozen"
    rows = [
        *_healthy_rows(),
        _frozen_row("BRK-B", 9.0),
        _frozen_row("DEADCO", 10.0),
        _frozen_row("GONECO", 11.0),
        _frozen_row("SPLITF", 12.0),
        _frozen_row("SPLITR", 13.0),
        _frozen_row("NOPE", 14.0, graded=False),
    ]
    for signal in SIGNALS:
        _write_frozen(root, signal, rows)
    return root


def _config(**overrides) -> PanelBuildConfig:
    defaults = dict(horizon_days=HORIZON, min_cross_section=5, min_periods=3)
    defaults.update(overrides)
    return PanelBuildConfig(**defaults)


# -- windows -------------------------------------------------------------------


def test_return_start_is_strictly_after_signal_date_and_windows_are_uniform(
    market_vault, frozen_root
):
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    panel = result.panel
    assert panel is not None
    for signal, group in panel.groupby("signal_date"):
        assert group["return_start"].nunique() == 1
        assert group["return_end"].nunique() == 1
        assert group["return_start"].iloc[0] > signal
    # And the evaluator's validator accepts it outright.
    evaluate_walk_forward(panel, BacktestConfig(min_cross_section=4, quantiles=2))


def test_overlapping_signals_are_skipped_not_double_counted():
    days = _weekdays(dt.date(2026, 8, 3), dt.date(2026, 9, 11))
    kept, skipped = select_non_overlapping(
        [dt.date(2026, 8, 3), dt.date(2026, 8, 5), dt.date(2026, 8, 12)], days, HORIZON
    )
    assert kept == [dt.date(2026, 8, 3), dt.date(2026, 8, 12)]
    assert skipped == [dt.date(2026, 8, 5)]


def test_window_for_returns_none_until_the_horizon_is_archived():
    days = _weekdays(dt.date(2026, 8, 3), dt.date(2026, 8, 7))
    assert window_for(dt.date(2026, 8, 3), days, horizon=21) is None
    assert window_for(dt.date(2026, 9, 1), days, horizon=1) is None


# -- the contract --------------------------------------------------------------


def test_built_panel_passes_the_backtest_input_contract_except_total_returns(
    market_vault, frozen_root
):
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    report = evaluate_walk_forward(result.panel, BacktestConfig(min_cross_section=4, quantiles=2))
    assert report.input_contract == {
        "filing_cutoff_provided": True,
        "point_in_time_universe_attested": True,
        "total_returns_attested": False,
        "delistings_included_attested": True,
        "permanent_identifier_present": True,
        # Added by M5 after the M1 spec was written; a single-profile build is
        # trivially single-universe.
        "single_universe_id": True,
    }
    assert any("does not attest" in note for note in report.limitations)


def test_delisted_name_is_priced_not_dropped(market_vault, frozen_root):
    """DEADCO vanishes from market_eod mid-window; its cohort history prices it."""
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    panel = result.panel
    first = panel[panel["signal_date"] == SIGNALS[0].isoformat()]

    dead = first[first["ticker"] == "DEADCO"].iloc[0]
    assert dead["return_source"] == "delisted_archive"
    assert dead["end_close"] == pytest.approx(7.5)  # column c, last row inside the window

    gone = first[first["ticker"] == "GONECO"].iloc[0]
    assert gone["return_source"] == "last_listed_close"
    assert bool(gone["terminal_price_used"]) is True

    # Row conservation: every graded frozen row is either kept or counted.
    accounting = result.periods[0]
    assert accounting.kept + accounting.ungraded_dropped == accounting.frozen_rows
    assert accounting.unresolved_dropped == 0


def test_missing_terminal_price_forces_delisting_attestation_false(market_vault, tmp_path):
    root = tmp_path / "frozen"
    _write_frozen(root, SIGNALS[0], [*_healthy_rows(), _frozen_row("LOSTCO", 9.5)])
    result = build_panel(
        root,
        "all_weather",
        market_vault,
        _Foundry(),
        _config(min_periods=1, max_unresolved_fraction=0.5),
        today=TODAY,
    )
    assert result.unresolved_rows == 1
    assert result.attestations["delisting_return_included"] is False
    assert result.attestations["universe_is_pit"] is False  # outcome-dependent drop
    assert "LOSTCO" in result.unresolved_tickers


# -- splits --------------------------------------------------------------------


def test_split_confirmed_by_foundry_is_divided_out(market_vault, frozen_root):
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    row = result.panel[
        (result.panel["ticker"] == "SPLITF")
        & (result.panel["signal_date"] == SIGNALS[0].isoformat())
    ].iloc[0]
    assert row["split_source"] == "foundry"
    assert row["split_factor"] == pytest.approx(2.0)
    # Economic return near flat, not a fabricated -50%.
    assert abs(row["forward_return"]) < 0.05


def test_split_reconstructed_from_volume_signature(market_vault, frozen_root):
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    row = result.panel[
        (result.panel["ticker"] == "SPLITR")
        & (result.panel["signal_date"] == SIGNALS[0].isoformat())
    ].iloc[0]
    assert row["split_source"] == "reconstructed"
    assert row["split_factor"] == pytest.approx(2.0)
    assert abs(row["forward_return"]) < 0.05


def test_uncorroborated_halving_is_excluded_not_guessed(market_vault, tmp_path):
    """CRASHCO halves with volume AND transactions spiking: crash-shaped, excluded."""
    root = tmp_path / "frozen"
    _write_frozen(root, SIGNALS[0], [*_healthy_rows(), _frozen_row("CRASHCO", 9.9)])
    result = build_panel(
        root,
        "all_weather",
        market_vault,
        _Foundry(),
        _config(min_periods=1, max_unresolved_fraction=0.5),
        today=TODAY,
    )
    assert result.periods[0].unresolved_dropped == 1
    assert "CRASHCO" not in set(result.panel["ticker"]) if result.panel is not None else True
    assert "CRASHCO" in result.unresolved_tickers


def test_detect_split_ignores_ordinary_volatility():
    assert detect_split(100.0, 90.0, tolerance=0.01) is None  # -10%
    assert detect_split(100.0, 50.2, tolerance=0.01) == pytest.approx(2.0)
    assert detect_split(10.0, 99.8, tolerance=0.01) == pytest.approx(0.1)  # 1-for-10 reverse


# -- spelling ------------------------------------------------------------------


def test_sec_dash_ticker_finds_polygon_dot_symbol(market_vault, frozen_root):
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    row = result.panel[
        (result.panel["ticker"] == "BRK-B")
        & (result.panel["signal_date"] == SIGNALS[0].isoformat())
    ].iloc[0]
    assert row["price_symbol"] == "BRK.B"
    assert row["forward_return"] == pytest.approx(
        (470.0 + 0.2 * 6) / (470.0 + 0.2 * 1) - 1.0, abs=1e-9
    )


# -- exclusions and refusals ---------------------------------------------------


def test_ungraded_rows_excluded_by_default_and_counted(market_vault, frozen_root):
    result = build_panel(frozen_root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    assert all(p.ungraded_dropped == 1 for p in result.periods)  # NOPE is ungraded
    assert "NOPE" not in set(result.panel["ticker"])


def test_backfilled_signal_date_before_forward_epoch_is_refused(market_vault, tmp_path):
    root = tmp_path / "frozen"
    backfilled = FORWARD_EPOCH - dt.timedelta(days=30)
    _write_frozen(root, backfilled, _healthy_rows())
    for signal in SIGNALS:
        _write_frozen(root, signal, _healthy_rows())

    refused = build_panel(root, "all_weather", market_vault, _Foundry(), _config(), today=TODAY)
    assert refused.refusal is not None and "forward epoch" in refused.refusal

    allowed = build_panel(
        root,
        "all_weather",
        market_vault,
        _Foundry(),
        _config(allow_backfilled_panels=True),
        today=TODAY,
    )
    assert allowed.refusal is None
    assert allowed.attestations["universe_is_pit"] is False


def test_stale_eod_archive_refuses_to_build(market_vault, frozen_root):
    result = build_panel(
        frozen_root,
        "all_weather",
        market_vault,
        _Foundry(),
        _config(),
        today=dt.date(2026, 10, 1),  # archive ends 09-11: stale
    )
    assert result.refusal is not None and "stale" in result.refusal


def test_stale_freeze_clock_refuses_to_build(market_vault, tmp_path):
    root = tmp_path / "frozen"
    _write_frozen(root, SIGNALS[0], _healthy_rows())  # newest signal 08-03
    result = build_panel(
        root,
        "all_weather",
        market_vault,
        _Foundry(),
        _config(max_eod_lag_days=60, max_freeze_age_days=30),
        today=dt.date(2026, 9, 20),  # 48 days after the newest signal
    )
    assert result.refusal is not None and "freeze clock" in result.refusal


def test_duplicate_cik_in_one_signal_date_is_refused_with_names(market_vault, tmp_path):
    root = tmp_path / "frozen"
    shared = "0009999999"
    rows = [
        *_healthy_rows(),
        _frozen_row("SPLITF", 8.0, cik=shared),
        _frozen_row("SPLITR", 8.5, cik=shared),
    ]
    _write_frozen(root, SIGNALS[0], rows)
    with pytest.raises(PanelBuildError) as excinfo:
        build_panel(root, "all_weather", market_vault, _Foundry(), _config(min_periods=1), today=TODAY)
    assert "SPLITF" in str(excinfo.value) and "SPLITR" in str(excinfo.value)


# -- maturity states -----------------------------------------------------------


def test_zero_matured_panels_writes_sidecar_and_exits_zero(market_vault, tmp_path):
    root = tmp_path / "frozen"
    _write_frozen(root, dt.date(2026, 9, 10), _healthy_rows())  # entry 09-11, no horizon
    config = _config()
    result = build_panel(root, "all_weather", market_vault, _Foundry(), config, today=TODAY)
    assert result.refusal is None
    assert result.matured_signal_dates == []
    assert result.pending_signal_dates == ["2026-09-10"]

    panel_path, sidecar = write_panel(result, tmp_path / "out", "all_weather", config)
    assert panel_path is None
    payload = json.loads(sidecar.read_text())
    assert payload["ready_for_backtest"] is False
    assert payload["periods"] == []


def test_fewer_than_min_periods_writes_panel_but_is_not_ready(market_vault, tmp_path):
    root = tmp_path / "frozen"
    for signal in SIGNALS[:2]:
        _write_frozen(root, signal, _healthy_rows())
    config = _config()  # min_periods=3
    result = build_panel(root, "all_weather", market_vault, _Foundry(), config, today=TODAY)
    assert len(result.periods) == 2
    assert result.ready_for_backtest is False

    panel_path, sidecar = write_panel(result, tmp_path / "out", "all_weather", config)
    assert panel_path is not None and panel_path.exists()
    assert json.loads(sidecar.read_text())["ready_for_backtest"] is False
