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
    for signal_date, scores in zip(
        signal_dates[: len(scores_by_month)], scores_by_month, strict=False
    ):
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


def test_each_horizon_is_a_separate_ledger_trial_with_one_shared_denominator(decay_world, tmp_path):
    frozen_dir, vault_root, _ = decay_world
    ledger = tmp_path / "ledger.jsonl"
    curve, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    record_sweep_trials(curve, ledger_path=ledger)
    records = load_manifest(ledger)
    assert [r["horizons"] for r in records] == [[5], [21], [63]]
    assert all(r["trials"] == 3 for r in records), "one shared, order-independent denominator"
    assert verify_chain(records) and all(verify_line(r) for r in records)

    # Re-run: three more records, all charged for every look ever recorded.
    curve2, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
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
    curve, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    record_sweep_trials(curve, ledger_path=ledger)
    for record in load_manifest(ledger):
        horizon = record["horizons"][0]
        if horizon != CONFIG.primary_horizon:
            assert record["gate_passed"] is False
            assert record["verdict"].startswith("EXPLORATORY")
        else:
            assert record["verdict"].startswith("PRIMARY (pre-declared)")


def test_ledger_sharpe_is_offset_averaged_and_significance_is_most_conservative(
    decay_world, tmp_path
):
    from stock_grader.significance import assess_edge, per_period_sharpe

    frozen_dir, vault_root, _ = decay_world
    ledger = tmp_path / "ledger.jsonl"
    curve, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    record_sweep_trials(curve, ledger_path=ledger)
    long = next(r for r in curve.horizons if r.horizon_days == 63)
    per_offset = [[p.net_spread for p in report.periods] for report in long.inference_by_offset]
    offset_sharpes = [per_period_sharpe(spreads) for spreads in per_offset]

    record = next(r for r in load_manifest(ledger) if r["horizons"] == [63])
    assert record["metrics"]["per_period_sharpe"] == pytest.approx(
        sum(offset_sharpes) / len(offset_sharpes)
    ), "the trial Sharpe must be the equal-weight average over ALL offsets"
    assert "fixed offset 0" not in record["leakage_controls"]
    assert "most conservative offset" in record["leakage_controls"]
    assert "Jegadeesh-Titman" in record["leakage_controls"]

    # The recorded significance is the most conservative offset's assessment:
    # every offset's DSR is at least the recorded one, so no phase choice
    # could have flipped the gate.
    trial_sharpes = [r["metrics"]["per_period_sharpe"] for r in load_manifest(ledger)]
    replayed = [
        assess_edge(
            spreads,
            trial_sharpes,
            periods_per_year=long.inference_by_offset[0].config.periods_per_year,
            bootstrap_seed=curve.config.seed,
        )
        for spreads in per_offset
    ]
    assert long.significance is not None
    assert long.significance.deflated_sharpe == pytest.approx(
        min(a.deflated_sharpe for a in replayed)
    )
    assert long.significance.significant == all(a.significant for a in replayed)


# -- decay shape ---------------------------------------------------------------


def test_planted_decay_curve_shows_higher_ic_per_day_at_short_horizons(decay_world):
    frozen_dir, vault_root, _ = decay_world
    curve, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    by_horizon = {r.horizon_days: r for r in curve.horizons if r.unusable_reason is None}
    assert 5 in by_horizon and 21 in by_horizon
    assert by_horizon[5].mean_rank_ic > 0, "the planted signal must be detected"
    assert by_horizon[5].ic_per_sqrt_day > by_horizon[63].ic_per_sqrt_day, (
        "a decaying signal must carry more IC per day at short horizons"
    )


def test_inference_averages_every_offset_subsample_not_an_arbitrary_phase(decay_world):
    """Jegadeesh-Titman: every signal date is used in exactly one offset
    subsample, and the inference numbers are the equal-weight average over ALL
    offsets — no statistic may depend on the old arbitrary fixed offset 0."""
    frozen_dir, vault_root, _ = decay_world
    curve, panels = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    by_horizon = {r.horizon_days: r for r in curve.horizons if r.unusable_reason is None}

    long = by_horizon[63]
    assert long.overlap_periods == 3
    assert len(long.inference_by_offset) == 3
    dates_by_offset = [
        [p.signal_date for p in report.periods] for report in long.inference_by_offset
    ]
    union = sorted(date for dates in dates_by_offset for date in dates)
    assert union == sorted(set(union)), "offset subsamples must be disjoint"
    assert union == sorted(str(d)[:10] for d in panels[63]["signal_date"].unique()), (
        "the union of offsets must use every signal date exactly once"
    )
    for dates in dates_by_offset:
        gaps = np.diff([np.datetime64(d) for d in dates]).astype("timedelta64[D]")
        assert (gaps.astype(int) >= 63).all(), "each subsample stays non-overlapping"

    per_offset_ir = [r.rank_ic_information_ratio for r in long.inference_by_offset]
    assert long.rank_ic_information_ratio == pytest.approx(sum(per_offset_ir) / len(per_offset_ir))
    per_offset_spread = [r.mean_net_spread for r in long.inference_by_offset]
    assert long.mean_net_spread == pytest.approx(sum(per_offset_spread) / len(per_offset_spread))
    per_offset_sharpe = [r.annualized_spread_sharpe for r in long.inference_by_offset]
    assert long.annualized_spread_sharpe == pytest.approx(
        sum(per_offset_sharpe) / len(per_offset_sharpe)
    )
    sizes = [len(r.periods) for r in long.inference_by_offset]
    assert long.effective_periods == round(sum(sizes) / len(sizes))

    # overlap == 1 keeps the single-subsample view bit-identical to before.
    short = by_horizon[5]
    assert short.overlap_periods == 1 and len(short.inference_by_offset) == 1
    only = short.inference_by_offset[0]
    assert short.rank_ic_information_ratio == pytest.approx(only.rank_ic_information_ratio)
    assert short.mean_net_spread == pytest.approx(only.mean_net_spread)
    assert short.effective_periods == len(only.periods) == short.periods


def test_non_overlapping_only_reports_offset_averaged_headline_stats(decay_world):
    frozen_dir, vault_root, _ = decay_world
    config = DecayConfig(
        horizons=(5, 21, 63),
        primary_horizon=21,
        min_cross_section=10,
        quantiles=2,
        non_overlapping_only=True,
    )
    curve, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=config)
    long = next(r for r in curve.horizons if r.horizon_days == 63)
    per_offset_ic = [r.mean_rank_ic for r in long.inference_by_offset]
    assert long.mean_rank_ic == pytest.approx(sum(per_offset_ic) / len(per_offset_ic))
    intervals = [r.rank_ic_interval for r in long.inference_by_offset]
    assert long.rank_ic_interval == (
        min(i[0] for i in intervals),
        max(i[1] for i in intervals),
    ), "the interval must be the ENVELOPE across offsets, never a tighter claim"


def _horizon_point(horizon: int, mean_ic: float, interval_width: float | None = None):
    from stock_grader.decay import HorizonResult

    result = HorizonResult(horizon_days=horizon, mean_rank_ic=mean_ic)
    if interval_width is not None:
        result.rank_ic_interval = (
            mean_ic - interval_width / 2.0,
            mean_ic + interval_width / 2.0,
        )
    return result


def test_half_life_refuses_when_ic_does_not_decay():
    rising = [_horizon_point(h, ic) for h, ic in ((5, 0.01), (21, 0.02), (63, 0.04))]
    half_life, interval, _, note = fit_half_life(rising)
    assert half_life is None and interval is None and "does not decay" in note

    too_few = [_horizon_point(5, 0.05)]
    half_life, interval, r2, note = fit_half_life(too_few)
    assert half_life is None and interval is None and r2 is None and "too few" in note

    zigzag = [_horizon_point(h, ic) for h, ic in ((5, 0.10), (21, 0.01), (63, 0.09), (126, 0.008))]
    half_life, interval, r2, note = fit_half_life(zigzag)
    assert half_life is None and interval is None and "fits poorly" in note
    assert r2 is not None and r2 < 0.5


def test_half_life_weighted_fit_downweights_a_noisy_point_and_reports_an_interval():
    """A wide-interval outlier must not steer the fit (finding: weighted fit)."""

    def planted(h: float) -> float:  # planted half-life: 20 trading days
        return 0.10 * 2.0 ** (-h / 20.0)

    tight = [_horizon_point(h, planted(h), 0.004) for h in (5, 21, 63)]
    outlier_ic = 0.02  # ~16x above the planted curve at 126d
    weighted_points = [*tight, _horizon_point(126, outlier_ic, 0.08)]
    half_life, interval, r2, note = fit_half_life(weighted_points)
    assert half_life == pytest.approx(20.0, rel=0.10)
    assert "inverse-variance weighted" in note and r2 is not None and r2 >= 0.5
    assert interval is not None and interval[0] < half_life < interval[1]
    assert interval[0] > 0

    # The same points WITHOUT intervals fall back to unweighted: the outlier
    # steers the fit far from the planted 20d and no interval is invented.
    unweighted_points = [_horizon_point(h, planted(h)) for h in (5, 21, 63)] + [
        _horizon_point(126, outlier_ic)
    ]
    half_life_u, interval_u, _, note_u = fit_half_life(unweighted_points)
    assert half_life_u is not None and interval_u is None
    assert "unweighted" in note_u
    assert abs(half_life - 20.0) < abs(half_life_u - 20.0)


def test_half_life_interval_is_withheld_when_decay_rate_is_indistinguishable_from_zero():
    """Perfect but glacial decay with wide IC intervals: the point estimate is
    reported, the 95% interval is unbounded above and therefore NOT reported."""
    points = [_horizon_point(h, 0.10 * math.exp(-0.0005 * h), 0.08) for h in (5, 21, 63)]
    half_life, interval, r2, note = fit_half_life(points)
    assert half_life is not None and half_life > 504, "glacial decay, huge half-life"
    assert interval is None and "unbounded above" in note
    assert r2 == pytest.approx(1.0), "the refusal to bound is about noise, not fit"


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
        [
            "decay",
            "--vault",
            "v",
            "--horizons",
            "5",
            "21",
            "--primary-horizon",
            "21",
            "--ledger",
            "l.jsonl",
        ]
    )
    assert args.primary_horizon == 21 and args.vault == "v"
    with pytest.raises(SystemExit):
        # --asof is reserved by main(); decay must not define it.
        build_parser().parse_args(["decay", "--vault", "v", "--asof", "2026-01-01"])


def test_decay_artifact_manifest_hashes_every_emitted_file(decay_world, tmp_path):
    frozen_dir, vault_root, _ = decay_world
    curve, panels = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    curve.ledger = record_sweep_trials(curve, ledger_path=tmp_path / "ledger.jsonl")
    out_dir = write_decay_artifacts(curve, panels, tmp_path / "signal_decay")
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == "1.1"
    assert "PRIVATE" in manifest["license_note"]
    listed = {f["name"]: f for f in manifest["files"]}
    for path in out_dir.iterdir():
        if path.name == "manifest.json":
            continue
        entry = listed[path.name]
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    decay_payload = json.loads((out_dir / "decay.json").read_text())
    assert "half_life_interval" in decay_payload
    for entry in decay_payload["horizons"]:
        assert "inference_report" not in entry, "1.1 replaced the fixed-offset report"
        reports = entry["inference_reports_by_offset"]
        assert len(reports) == entry["overlap_periods"], "one report per offset"


def test_decay_markdown_reports_curve_half_life_power_and_trial_charge(decay_world, tmp_path):
    frozen_dir, vault_root, _ = decay_world
    curve, _ = evaluate_decay(frozen_dir, vault_root, profile="all_weather", config=CONFIG)
    curve.ledger = record_sweep_trials(curve, ledger_path=tmp_path / "ledger.jsonl")
    markdown = decay_to_markdown(curve)
    assert "# Signal decay — all_weather" in markdown
    assert "not investment advice" in markdown.lower() or "not a recommendation" in markdown.lower()
    assert "PRIMARY" in markdown and "exploratory" in markdown
    assert "OVER-deflates" in markdown or "over-deflates" in markdown
    assert "| horizon |" in markdown or "| 5d |" in markdown
    assert "#" * 3 in markdown  # the ASCII bar
    assert "Jegadeesh–Titman" in markdown and "most conservative offset" in markdown
    if curve.half_life_days is not None:
        assert curve.half_life_note in markdown, "the fit's own caveat must be shown"
        if curve.half_life_interval is not None:
            assert "95% interval [" in markdown
