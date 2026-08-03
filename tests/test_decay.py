"""Signal decay: window discipline, ledger charging, and honest refusals."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_grader.backtest import evaluate_walk_forward
from stock_grader.cli import build_parser
from stock_grader.data.vault import VaultDataSource
from stock_grader.decay import (
    DecayConfig,
    build_horizon_panel,
    decay_to_markdown,
    evaluate_decay,
    fit_half_life,
    load_frozen_panels,
    record_sweep_trials,
    write_decay_artifacts,
)
from stock_grader.research_manifest import load_manifest, verify_chain, verify_line

# -- fixtures ------------------------------------------------------------------

RNG = np.random.default_rng(7)
TICKERS = [f"T{i:02d}" for i in range(60)]


def _sessions(start: dt.date, count: int) -> list[dt.date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _build_vault(root: Path, sessions: list[dt.date], closes: dict[str, list[float]]) -> Path:
    by_month: dict[str, list[str]] = {}
    for index, day in enumerate(sessions):
        month = day.strftime("%Y-%m")
        directory = root / "data" / "market_eod" / month
        directory.mkdir(parents=True, exist_ok=True)
        rows = []
        for symbol, series in sorted(closes.items()):
            value = series[index]
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue
            rows.append(
                json.dumps(
                    {
                        "symbol": symbol,
                        "open": value,
                        "high": value,
                        "low": value,
                        "close": value,
                        "volume": 1e6,
                        "vwap": value,
                        "transactions": 1000,
                    },
                    sort_keys=True,
                )
            )
        name = f"{day.isoformat()}.jsonl.gz"
        (directory / name).write_bytes(gzip.compress(("\n".join(rows) + "\n").encode()))
        by_month.setdefault(month, []).append(name)
    for month, names in by_month.items():
        directory = root / "data" / "market_eod" / month
        files = []
        for name in sorted(names):
            blob = (directory / name).read_bytes()
            files.append(
                {"name": name, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
            )
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "source_urls": [],
                    "license_note": "fixture",
                    "files": files,
                }
            )
        )
    return root


def _write_frozen(
    root: Path,
    profile: str,
    signal_date: dt.date,
    scores: dict[str, float],
    *,
    config_fp: str = "cfg",
) -> None:
    directory = root / profile
    directory.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "signal_date": signal_date.isoformat(),
                "ticker": ticker,
                "cik": str(i + 1).zfill(10),
                "score": score,
                "letter": "B",
                "percentile": 50.0,
                "coverage": 0.9,
                "graded": True,
                "profile": profile,
                "config_fingerprint": config_fp,
                "universe_fingerprint": "uni",
                "code_commit": "fixture",
                "schema_version": "1.0",
            }
            for i, (ticker, score) in enumerate(sorted(scores.items()))
        ]
    ).to_parquet(directory / f"{signal_date.isoformat()}.parquet", index=False)


@pytest.fixture(scope="module")
def decay_world(tmp_path_factory) -> tuple[Path, Path, list[dt.date]]:
    """~14 monthly signal dates, 60 tickers, IC planted to decay with horizon.

    Score predicts the next ~21 sessions' return strongly, then the signal
    washes out — so short horizons carry more IC per day than long ones.
    """
    root = tmp_path_factory.mktemp("decay")
    sessions = _sessions(dt.date(2025, 1, 2), 400)
    scores_by_month: list[dict[str, float]] = []
    signal_dates = [sessions[i] for i in range(0, 320, 21)]

    closes: dict[str, list[float]] = {t: [100.0] for t in TICKERS}
    signal_for: dict[str, float] = dict.fromkeys(TICKERS, 0.0)
    next_signal = 0
    for index in range(1, len(sessions)):
        day = sessions[index]
        if next_signal < len(signal_dates) and day > signal_dates[next_signal]:
            fresh = {t: float(RNG.standard_normal()) for t in TICKERS}
            signal_for = fresh
            scores_by_month.append(fresh)
            next_signal += 1
        for ticker in TICKERS:
            drift = 0.004 * signal_for.get(ticker, 0.0)  # signal pays off...
            signal_for[ticker] *= 0.93  # ...and decays (~10-session half-life)
            noise = float(RNG.standard_normal()) * 0.01
            closes[ticker].append(closes[ticker][-1] * (1.0 + drift + noise))

    _build_vault(root, sessions, closes)
    frozen = root / "frozen"
    for signal_date, scores in zip(signal_dates[: len(scores_by_month)], scores_by_month,
                                   strict=False):
        _write_frozen(frozen, "all_weather", signal_date, scores)
    return frozen / "all_weather", root, sessions


CONFIG = DecayConfig(horizons=(5, 21, 63), primary_horizon=21, min_cross_section=10, quantiles=2)


# -- close matrix --------------------------------------------------------------


def test_close_matrix_reads_each_day_once_and_bridges_ticker_spellings(tmp_path):
    sessions = _sessions(dt.date(2026, 7, 1), 3)
    _build_vault(tmp_path, sessions, {"BRK.B": [470.0, 471.0, 472.0]})
    vault = VaultDataSource(tmp_path)
    opened: list[str] = []
    original = VaultDataSource._read_verified

    def counting(self, dataset, name):
        opened.append(name)
        return original(self, dataset, name)

    VaultDataSource._read_verified = counting
    try:
        matrix = vault.market_eod_close_matrix(["BRK-B"])
    finally:
        VaultDataSource._read_verified = original
    assert list(matrix.columns) == ["BRK-B"]
    assert matrix["BRK-B"].iloc[0] == pytest.approx(470.0)
    assert len(opened) == len(set(opened)) == 3, "each day file must be read exactly once"


# -- window discipline ---------------------------------------------------------


def test_horizon_panel_passes_backtest_validation_and_window_chronology(decay_world):
    frozen_dir, vault_root, _ = decay_world
    frozen = load_frozen_panels(frozen_dir)
    closes = VaultDataSource(vault_root).market_eod_close_matrix(sorted(TICKERS))
    for horizon in (5, 21):
        panel, _ = build_horizon_panel(frozen, closes, horizon, config=CONFIG)
        assert len(panel)
        assert (panel["return_start"] > panel["signal_date"]).all()
        assert (panel["return_end"] > panel["return_start"]).all()
        evaluate_walk_forward(panel)


def test_one_signal_date_maps_to_exactly_one_return_window_per_panel(decay_world):
    """The one-file-per-horizon layout is FORCED by backtest.py, not chosen."""
    frozen_dir, vault_root, _ = decay_world
    frozen = load_frozen_panels(frozen_dir)
    closes = VaultDataSource(vault_root).market_eod_close_matrix(sorted(TICKERS))
    panel_5, _ = build_horizon_panel(frozen, closes, 5, config=CONFIG)
    panel_21, _ = build_horizon_panel(frozen, closes, 21, config=CONFIG)
    with pytest.raises(ValueError, match="mixes return windows|duplicate"):
        evaluate_walk_forward(pd.concat([panel_5, panel_21], ignore_index=True))


def test_missing_exit_price_is_dropped_and_counted_not_silently_survivorship_filtered(
    tmp_path,
):
    sessions = _sessions(dt.date(2026, 1, 5), 30)
    closes = {t: [100.0 + i * 0.1 for i in range(30)] for t in ("AAA", "BBB", "CCC", "DDD")}
    closes["DEADCO"] = [50.0] * 10 + [None] * 20  # stops trading mid-window
    _build_vault(tmp_path, sessions, closes)
    frozen_root = tmp_path / "frozen"
    _write_frozen(
        frozen_root,
        "all_weather",
        sessions[2],
        {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0, "DDD": 4.0, "DEADCO": 5.0},
    )
    frozen = load_frozen_panels(frozen_root / "all_weather")
    matrix = VaultDataSource(tmp_path).market_eod_close_matrix(
        ["AAA", "BBB", "CCC", "DDD", "DEADCO"]
    )
    config = DecayConfig(horizons=(21,), primary_horizon=21, min_cross_section=4, quantiles=2)
    panel, counts = build_horizon_panel(frozen, matrix, 21, config=config)
    assert counts["dropped_missing_exit"] == 1
    assert "DEADCO" not in set(panel["ticker"])

    imputed = DecayConfig(
        horizons=(21,),
        primary_horizon=21,
        min_cross_section=4,
        quantiles=2,
        delisting_return=-0.30,
    )
    panel2, counts2 = build_horizon_panel(frozen, matrix, 21, config=imputed)
    dead = panel2[panel2["ticker"] == "DEADCO"]
    assert len(dead) == 1 and dead["forward_return"].iloc[0] == pytest.approx(-0.30)
    assert counts2["dropped_missing_exit"] == 1  # still counted


def test_split_suspect_screen_drops_unadjusted_split_jumps(tmp_path):
    sessions = _sessions(dt.date(2026, 1, 5), 30)
    closes = {t: [100.0] * 30 for t in ("AAA", "BBB", "CCC", "DDD")}
    closes["SPLITCO"] = [400.0] * 15 + [100.0] * 15  # 4:1, unadjusted
    _build_vault(tmp_path, sessions, closes)
    frozen_root = tmp_path / "frozen"
    _write_frozen(
        frozen_root,
        "all_weather",
        sessions[2],
        {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0, "DDD": 4.0, "SPLITCO": 5.0},
    )
    frozen = load_frozen_panels(frozen_root / "all_weather")
    matrix = VaultDataSource(tmp_path).market_eod_close_matrix(
        ["AAA", "BBB", "CCC", "DDD", "SPLITCO"]
    )
    config = DecayConfig(horizons=(21,), primary_horizon=21, min_cross_section=4, quantiles=2)
    panel, counts = build_horizon_panel(frozen, matrix, 21, config=config)
    assert counts["dropped_split_suspect"] == 1
    assert "SPLITCO" not in set(panel["ticker"])

    keep = DecayConfig(
        horizons=(21,),
        primary_horizon=21,
        min_cross_section=4,
        quantiles=2,
        split_screen=False,
    )
    panel2, _ = build_horizon_panel(frozen, matrix, 21, config=keep)
    assert "SPLITCO" in set(panel2["ticker"])


# -- ledger charging -----------------------------------------------------------


def test_each_horizon_is_a_separate_ledger_trial_with_one_shared_denominator(
    decay_world, tmp_path
):
    frozen_dir, vault_root, _ = decay_world
    ledger = tmp_path / "ledger.jsonl"
    curve, _ = evaluate_decay(
        frozen_dir, vault_root, profile="all_weather", config=CONFIG
    )
    record_sweep_trials(curve, ledger_path=ledger)
    records = load_manifest(ledger)
    assert [r["horizons"] for r in records] == [[5], [21], [63]]
    assert all(r["trials"] == 3 for r in records), "one shared, order-independent denominator"
    assert verify_chain(records) and all(verify_line(r) for r in records)

    # Re-run: three more records, all charged for every look ever recorded.
    curve2, _ = evaluate_decay(
        frozen_dir, vault_root, profile="all_weather", config=CONFIG
    )
    record_sweep_trials(curve2, ledger_path=ledger)
    records = load_manifest(ledger)
    assert len(records) == 6
    # trial_sharpes collapses repeats per experiment, so the denominator is
    # prior distinct experiments (3) + this sweep's looks (3).
    assert all(r["trials"] == 6 for r in records[3:])


def test_non_primary_horizons_are_marked_exploratory_and_cannot_pass_the_gate(
    decay_world, tmp_path
):
    frozen_dir, vault_root, _ = decay_world
    ledger = tmp_path / "ledger.jsonl"
    curve, _ = evaluate_decay(
        frozen_dir, vault_root, profile="all_weather", config=CONFIG
    )
    record_sweep_trials(curve, ledger_path=ledger)
    for record in load_manifest(ledger):
        horizon = record["horizons"][0]
        if horizon != CONFIG.primary_horizon:
            assert record["gate_passed"] is False
            assert record["verdict"].startswith("EXPLORATORY")
        else:
            assert record["verdict"].startswith("PRIMARY (pre-declared)")


# -- decay shape ---------------------------------------------------------------


def test_planted_decay_curve_shows_higher_ic_per_day_at_short_horizons(decay_world):
    frozen_dir, vault_root, _ = decay_world
    curve, _ = evaluate_decay(
        frozen_dir, vault_root, profile="all_weather", config=CONFIG
    )
    by_horizon = {r.horizon_days: r for r in curve.horizons if r.unusable_reason is None}
    assert 5 in by_horizon and 21 in by_horizon
    assert by_horizon[5].mean_rank_ic > 0, "the planted signal must be detected"
    assert by_horizon[5].ic_per_sqrt_day > by_horizon[63].ic_per_sqrt_day, (
        "a decaying signal must carry more IC per day at short horizons"
    )


def test_half_life_refuses_when_ic_does_not_decay():
    from stock_grader.decay import HorizonResult

    rising = [
        HorizonResult(horizon_days=h, mean_rank_ic=ic)
        for h, ic in ((5, 0.01), (21, 0.02), (63, 0.04))
    ]
    half_life, _, note = fit_half_life(rising)
    assert half_life is None and "does not decay" in note

    too_few = [HorizonResult(horizon_days=5, mean_rank_ic=0.05)]
    half_life, r2, note = fit_half_life(too_few)
    assert half_life is None and r2 is None and "too few" in note


def test_decay_refuses_when_no_horizon_has_a_completed_forward_window(tmp_path):
    sessions = _sessions(dt.date(2026, 7, 1), 10)
    closes = {t: [100.0] * 10 for t in ("AAA", "BBB", "CCC", "DDD")}
    _build_vault(tmp_path, sessions, closes)
    frozen_root = tmp_path / "frozen"
    _write_frozen(
        frozen_root,
        "all_weather",
        sessions[-1],
        {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0, "DDD": 4.0},
    )
    with pytest.raises(ValueError, match="no horizon has a completed forward window"):
        evaluate_decay(
            frozen_root / "all_weather",
            tmp_path,
            profile="all_weather",
            config=DecayConfig(
                horizons=(21,), primary_horizon=21, min_cross_section=4, quantiles=2
            ),
        )


def test_decay_refuses_mixed_fingerprints_without_override(tmp_path):
    frozen_root = tmp_path / "frozen"
    scores = {f"T{i}": float(i) for i in range(6)}
    _write_frozen(frozen_root, "all_weather", dt.date(2026, 6, 1), scores, config_fp="aaa")
    _write_frozen(frozen_root, "all_weather", dt.date(2026, 7, 1), scores, config_fp="bbb")
    with pytest.raises(ValueError, match="comparable only when fingerprints match"):
        load_frozen_panels(frozen_root / "all_weather")
    frame = load_frozen_panels(frozen_root / "all_weather", allow_fingerprint_drift=True)
    assert len(frame)


def test_decay_ignores_the_legacy_flat_frozen_panel(tmp_path):
    frozen_root = tmp_path / "frozen"
    scores = {f"T{i}": float(i) for i in range(6)}
    _write_frozen(frozen_root, "all_weather", dt.date(2026, 6, 1), scores)
    # A stray legacy flat panel beside the profile directories:
    pd.DataFrame(
        [{"signal_date": "2026-05-01", "ticker": "STRAY", "score": 1.0, "graded": True}]
    ).to_parquet(frozen_root / "2026-05-01.parquet", index=False)
    with pytest.raises(ValueError, match="profile subdirectory"):
        load_frozen_panels(frozen_root)
    frame = load_frozen_panels(frozen_root / "all_weather")
    assert "STRAY" not in set(frame["ticker"])


# -- CLI and artifacts ---------------------------------------------------------


def test_decay_cli_args_are_registered_on_the_subparser():
    args = build_parser().parse_args(
        ["decay", "--vault", "v", "--horizons", "5", "21", "--primary-horizon", "21",
         "--ledger", "l.jsonl"]
    )
    assert args.primary_horizon == 21 and args.vault == "v"
    with pytest.raises(SystemExit):
        # --asof is reserved by main(); decay must not define it.
        build_parser().parse_args(["decay", "--vault", "v", "--asof", "2026-01-01"])


def test_decay_artifact_manifest_hashes_every_emitted_file(decay_world, tmp_path):
    frozen_dir, vault_root, _ = decay_world
    curve, panels = evaluate_decay(
        frozen_dir, vault_root, profile="all_weather", config=CONFIG
    )
    curve.ledger = record_sweep_trials(curve, ledger_path=tmp_path / "ledger.jsonl")
    out_dir = write_decay_artifacts(curve, panels, tmp_path / "signal_decay")
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == "1.0"
    assert "PRIVATE" in manifest["license_note"]
    listed = {f["name"]: f for f in manifest["files"]}
    for path in out_dir.iterdir():
        if path.name == "manifest.json":
            continue
        entry = listed[path.name]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_decay_markdown_reports_curve_half_life_power_and_trial_charge(decay_world, tmp_path):
    frozen_dir, vault_root, _ = decay_world
    curve, _ = evaluate_decay(
        frozen_dir, vault_root, profile="all_weather", config=CONFIG
    )
    curve.ledger = record_sweep_trials(curve, ledger_path=tmp_path / "ledger.jsonl")
    markdown = decay_to_markdown(curve)
    assert "# Signal decay — all_weather" in markdown
    assert "not investment advice" in markdown.lower() or "not a recommendation" in markdown.lower()
    assert "PRIMARY" in markdown and "exploratory" in markdown
    assert "OVER-deflates" in markdown or "over-deflates" in markdown
    assert "| horizon |" in markdown or "| 5d |" in markdown
    assert "#" * 3 in markdown  # the ASCII bar
